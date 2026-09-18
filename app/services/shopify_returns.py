"""Native Shopify returns plus per-request order metadata. No gateway money movement.

The request row is the durable work record. Remote creation is never blindly retried
when the response is ambiguous; the request marker is reconciled first.
"""
import json
from flask import current_app
from app.extensions import db
from app.models import ReturnRequest, ExchangeRequest, OrderItem, RequestStatus
from app.services.shopify_client import graphql, mutation, ShopifyUserError, select_exchange_variant
from app.utils.request_status import stage_for

RETURN_FIELDS = """id status
 returnLineItems(first: 100) { nodes { ... on ReturnLineItem {
   id quantity processedQuantity processableQuantity returnReasonNote
   fulfillmentLineItem { id lineItem { id } }
 } } pageInfo { hasNextPage } }
 exchangeLineItems(first: 100) { nodes { id quantity processedQuantity processableQuantity variantId lineItems { id } } pageInfo { hasNextPage } }
 reverseFulfillmentOrders(first: 100) { nodes { lineItems(first: 100) { nodes { id fulfillmentLineItem { id } } pageInfo { hasNextPage } } } pageInfo { hasNextPage } }
"""


def gid(kind, ident):
    return str(ident) if str(ident).startswith("gid://") else f"gid://shopify/{kind}/{ident}"


def number(req, kind):
    return req.return_number if kind == "return" else req.exchange_number


def _read_return(ident):
    result = graphql("query($id: ID!) { return(id:$id) { " + RETURN_FIELDS + " } }", {"id":ident})["return"]
    if not result:
        raise ValueError("Linked Shopify return was not found; manual reconciliation required")
    for key in ("returnLineItems", "exchangeLineItems", "reverseFulfillmentOrders"):
        if result[key]["pageInfo"]["hasNextPage"]:
            raise ValueError("Linked Shopify return has unexpected extra lines; review in Shopify")
    return result


def _reconcile(req, kind):
    cursor = None
    marker = "Tanoti:" + number(req, kind) + " "
    matches = []
    while True:
        data = graphql("""query($id:ID!,$after:String) { order(id:$id) {
          returns(first:50,after:$after) { nodes { id returnLineItems(first:100) {
            nodes { ... on ReturnLineItem { returnReasonNote } } pageInfo {hasNextPage}
          } } pageInfo {hasNextPage endCursor} }
        } }""", {"id":gid("Order",req.order_item.order.shopify_order_id), "after":cursor})
        if not data["order"]:
            raise ValueError("Shopify order was not found")
        conn = data["order"]["returns"]
        for ret in conn["nodes"]:
            if ret["returnLineItems"]["pageInfo"]["hasNextPage"]:
                raise ValueError("Cannot safely reconcile a large existing return; review in Shopify")
            if any(line.get("returnReasonNote", "").startswith(marker) for line in ret["returnLineItems"]["nodes"]):
                matches.append(ret["id"])
        if not conn["pageInfo"]["hasNextPage"]:
            break
        cursor = conn["pageInfo"]["endCursor"]
    if len(matches) > 1:
        raise ValueError("Multiple Shopify returns match this request; manual reconciliation required")
    return matches[0] if matches else None


def _returnable(req):
    cursor = None
    while True:
        conn = graphql("""query($id:ID!,$after:String) {
          returnableFulfillments(orderId:$id,first:50,after:$after) {
            nodes { fulfillment { location { id } }
              returnableFulfillmentLineItems(first:250) { nodes { quantity fulfillmentLineItem { id lineItem { id } } } pageInfo {hasNextPage} }
            } pageInfo {hasNextPage endCursor}
          }
        }""", {"id":gid("Order",req.order_item.order.shopify_order_id), "after":cursor})["returnableFulfillments"]
        for fulfillment in conn["nodes"]:
            lines = fulfillment["returnableFulfillmentLineItems"]
            if lines["pageInfo"]["hasNextPage"]:
                raise ValueError("Fulfillment is too large to resolve safely; review in Shopify")
            for line in lines["nodes"]:
                if line["quantity"] > 0 and line["fulfillmentLineItem"]["lineItem"]["id"] == gid("LineItem",req.order_item.shopify_line_item_id):
                    location = (fulfillment.get("fulfillment") or {}).get("location") or {}
                    return line["fulfillmentLineItem"]["id"], location.get("id")
        if not conn["pageInfo"]["hasNextPage"]:
            raise ValueError("No fulfilled, returnable quantity is available in Shopify. Check fulfillment and existing returns.")
        cursor = conn["pageInfo"]["endCursor"]


def _create(req, kind):
    found = _reconcile(req, kind)
    if found:
        req.shopify_return_id = found
        return
    if req.shopify_create_attempted:
        raise ValueError("A previous Shopify creation had an uncertain result. No matching return was found. Review in Shopify before creating anything again.")
    fulfillment_id, location_id = _returnable(req)
    marker = "Tanoti:" + number(req, kind) + " "
    payload = {"orderId":gid("Order", req.order_item.order.shopify_order_id),
               "requestedAt":req.created_at.isoformat()+"Z",
               "returnLineItems":[{"fulfillmentLineItemId":fulfillment_id,"quantity":1,
                   "returnReason":"OTHER", "returnReasonNote":(marker+req.reason.value)[:255]}]}
    if kind == "exchange":
        if not req.requested_variant_id:
            req.requested_variant_id = select_exchange_variant(req.order_item, req.requested_size)["id"]
        payload["exchangeLineItems"] = [{"variantId":gid("ProductVariant",req.requested_variant_id),"quantity":1}]
    req.shopify_location_id = location_id
    req.shopify_create_attempted = True
    # Persist the marker BEFORE the non-idempotent remote write.
    db.session.commit()
    try:
        result = mutation("""mutation($input:ReturnInput!) { returnCreate(returnInput:$input) {
          return {id} userErrors {field message} } }""", {"input":payload}, "returnCreate")
    except ShopifyUserError:
        req.shopify_create_attempted = False
        raise
    req.shopify_return_id = result["return"]["id"]
    db.session.commit()


def _process(req, kind, remote):
    if remote["status"] == "CANCELED":
        raise ValueError("The linked return was canceled in Shopify; reconcile before continuing")
    lines = remote["returnLineItems"]["nodes"]
    if len(lines) != 1 or lines[0]["quantity"] != 1:
        raise ValueError("Shopify return lines were changed externally; review before processing")
    line = lines[0]
    returns = []
    if line["processedQuantity"] < 1:
        if req.restock is None:
            raise ValueError("Choose whether to restock this inspected item before Shopify processing")
        reverse_ids = []
        for group in remote["reverseFulfillmentOrders"]["nodes"]:
            if group["lineItems"]["pageInfo"]["hasNextPage"]:
                raise ValueError("Unexpected reverse fulfillment size; review in Shopify")
            reverse_ids.extend(r["id"] for r in group["lineItems"]["nodes"] if r["fulfillmentLineItem"]["id"] == line["fulfillmentLineItem"]["id"])
        if len(reverse_ids) != 1:
            raise ValueError("Could not identify the returned unit for a restock decision")
        disposition = {"reverseFulfillmentOrderLineItemId":reverse_ids[0], "quantity":1,
                       "dispositionType":"RESTOCKED" if req.restock else "NOT_RESTOCKED"}
        if req.restock:
            if not req.shopify_location_id:
                raise ValueError("Original Shopify stock location is unknown; review restocking in Shopify")
            disposition["locationId"] = req.shopify_location_id
        returns = [{"id":line["id"],"quantity":1,"dispositions":[disposition]}]
    exchanges = []
    if kind == "exchange":
        exchanges = [{"id":line["id"],"quantity":1} for line in remote["exchangeLineItems"]["nodes"] if line["processedQuantity"] < 1]
        if len(remote["exchangeLineItems"]["nodes"]) != 1:
            raise ValueError("The Shopify exchange does not contain exactly one replacement")
    if returns or exchanges:
        mutation("""mutation($input:ReturnProcessInput!) { returnProcess(input:$input) {
          return {id} userErrors {field message} } }""", {"input":{"returnId":req.shopify_return_id,
          "returnLineItems":returns, "exchangeLineItems":exchanges, "notifyCustomer":False,
          "note":"Tanoti " + number(req,kind) + ": payment/gift-card outcome recorded separately; do not issue a duplicate refund."}}, "returnProcess")
        remote = _read_return(req.shopify_return_id)
    if kind == "exchange":
        linked = remote["exchangeLineItems"]["nodes"][0].get("lineItems") or []
        if len(linked) != 1:
            raise ValueError("Shopify has not provided a unique replacement order line yet; retry sync")
        req.replacement_line_id = linked[0]["id"].rsplit("/",1)[-1]
        if req.replacement_line_id == req.order_item.shopify_line_item_id:
            raise ValueError("Shopify reused the original line for this replacement; unit mapping needs manual review")
        from app.services.replacement_service import ensure_replacement
        item = ensure_replacement(req)
        if item:
            # Hide an unclaimed duplicate imported by an early webhook; preserve its identity.
            duplicates = OrderItem.query.filter_by(order_id=item.order_id, shopify_line_item_id=req.replacement_line_id, replacement_for=None).all()
            for duplicate in duplicates:
                if duplicate.return_requests or duplicate.exchange_requests or duplicate.request_claimed:
                    raise ValueError("Replacement line already has an unrelated request; manual reconciliation required")
                duplicate.superseded = True
                duplicate.unavailable = True
            item.shopify_line_item_id = req.replacement_line_id


def _metadata(req, kind):
    data = {"request":number(req,kind), "type":kind, "stage":stage_for(req,kind),
            "unit":req.order_item.unit_number, "line_item_id":req.order_item.shopify_line_item_id,
            "replacement_for":req.order_item.replacement_for, "customer_note":req.customer_note,
            "return_id":req.shopify_return_id, "rejection_reason":req.rejection_reason.value if req.rejection_reason else None,
            "rejection_note":req.rejection_note}
    if kind == "return":
        data.update(refund_amount=str(req.net_refund_amount), refund_mode=req.refund_mode.value,
                    refund_paid_at=req.refund_paid_at.isoformat() if req.refund_paid_at else None,
                    gift_card_issued=bool(req.gift_card_code))
    else:
        data.update(requested_variant_id=req.requested_variant_id, replacement_line_id=req.replacement_line_id,
                    delivered_at=req.delivered_at.isoformat() if req.delivered_at else None)
    mutation("""mutation($metafields:[MetafieldsSetInput!]!) { metafieldsSet(metafields:$metafields) {
      metafields {id} userErrors {field message} } }""", {"metafields":[{
      "ownerId":gid("Order",req.order_item.order.shopify_order_id), "namespace":"tanoti_returns",
      "key":number(req,kind).lower().replace("-","_"),"type":"json", "value":json.dumps(data)}]}, "metafieldsSet")
    mutation("""mutation($id:ID!,$tags:[String!]!) { tagsAdd(id:$id,tags:$tags) { userErrors {field message} } }""",
             {"id":gid("Order",req.order_item.order.shopify_order_id), "tags":["Tanoti "+kind, number(req,kind)]}, "tagsAdd")


def sync_one(req, kind):
    """Call only after business-state commit. Failure remains visible and retryable."""
    model = ReturnRequest if kind == "return" else ExchangeRequest
    ident = req.id
    req = model.query.filter_by(id=ident).with_for_update().populate_existing().one()
    try:
        if not current_app.config.get("SHOPIFY_STORE_DOMAIN"):
            raise ValueError("Shopify store is not configured")
        # Marker is reconciled even when a rejected request had an ambiguous creation.
        if req.status not in (RequestStatus.PENDING, RequestStatus.REJECTED):
            if not req.shopify_return_id:
                _create(req, kind)
            req = model.query.filter_by(id=req.id).with_for_update().populate_existing().one()
            remote = _read_return(req.shopify_return_id)
            if req.status == RequestStatus.REJECTED and remote["status"] != "CANCELED":
                mutation("mutation($id:ID!) { returnCancel(id:$id) { return {id} userErrors {field message} } }",
                         {"id":req.shopify_return_id}, "returnCancel")
            if req.status == RequestStatus.COMPLETED:
                _process(req, kind, remote)
        elif req.status == RequestStatus.REJECTED:
            if not req.shopify_return_id and req.shopify_create_attempted:
                req.shopify_return_id = _reconcile(req,kind)
                if not req.shopify_return_id:
                    raise ValueError("Uncertain Shopify creation needs manual reconciliation")
            if req.shopify_return_id:
                remote = _read_return(req.shopify_return_id)
                if remote["status"] != "CANCELED":
                    mutation("mutation($id:ID!) { returnCancel(id:$id) { return {id} userErrors {field message} } }",
                             {"id":req.shopify_return_id}, "returnCancel")
        _metadata(req, kind)
        req.shopify_sync_status = "synced"
        req.shopify_sync_error = None
        req.shopify_sync_stage = stage_for(req, kind)
    except Exception as exc:
        # Recover a failed SQL transaction as well as a remote API failure. Remote
        # outcomes are re-read on the next attempt; never roll back business state.
        message = str(exc)[:1000]
        db.session.rollback()
        req = model.query.filter_by(id=ident).with_for_update().populate_existing().one()
        if isinstance(exc, ShopifyUserError) and not req.shopify_return_id:
            req.shopify_create_attempted = False
        req.shopify_sync_status = "needs_review" if req.shopify_create_attempted and not req.shopify_return_id else "error"
        req.shopify_sync_error = message
        current_app.logger.warning("Shopify sync pending for %s", number(req,kind))
    db.session.commit()
    return req


def retry_pending(limit=50):
    for model, kind in ((ReturnRequest,"return"),(ExchangeRequest,"exchange")):
        ids = [r.id for r in model.query.filter(model.shopify_sync_status != "synced").order_by(model.id).limit(limit)]
        for ident in ids:
            sync_one(db.session.get(model,ident),kind)
