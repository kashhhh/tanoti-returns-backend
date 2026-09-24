"""
Turns a Shopify order payload (from a webhook or a REST pull) into local
Customer / Order / OrderItem rows. Idempotent: re-running with the same
order just updates the existing rows rather than duplicating them.
"""
from datetime import datetime, timezone
import hashlib

from sqlalchemy import text

from app.extensions import db
from app.models import Customer, Order, OrderItem, PaymentMethod, ExchangeRequest, RequestStatus




def _utc(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone(timezone.utc).replace(tzinfo=None) if parsed.tzinfo else parsed


def _parse_payment_method(shopify_order: dict) -> PaymentMethod:
    gateways = [g.lower() for g in shopify_order.get("payment_gateway_names", [])]
    if any("cash on delivery" in g or g == "cod" for g in gateways):
        return PaymentMethod.COD
    return PaymentMethod.PREPAID


def _customer_lock_key(value: str) -> int:
    """Return a stable signed bigint suitable for a Postgres advisory lock."""
    digest = hashlib.sha256(value.encode("utf-8")).digest()[:8]
    return int.from_bytes(digest, byteorder="big", signed=True)


def _lock_customer_identity(shopify_customer_id: str, email: str) -> None:
    """Serialize first-time customer creation across Gunicorn threads.

    Customer identity has two unique keys. Lock both, in a stable order, so
    concurrent order webhooks cannot both observe a missing row and attempt
    to insert it. SQLite is used by the isolated test suite and processes its
    writes serially, so the Postgres-specific lock is deliberately skipped.
    """
    if db.session.get_bind().dialect.name != "postgresql":
        return

    keys = {
        _customer_lock_key(f"shopify-customer-id:{shopify_customer_id}"),
        _customer_lock_key(f"shopify-customer-email:{email}"),
    }
    for key in sorted(keys):
        db.session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


def upsert_customer(shopify_customer: dict) -> Customer:
    email = (shopify_customer.get("email") or "").strip().lower()
    shopify_customer_id = str(shopify_customer["id"])
    _lock_customer_identity(shopify_customer_id, email)

    customer_by_id = Customer.query.filter_by(shopify_customer_id=shopify_customer_id).first()
    customer_by_email = Customer.query.filter_by(email=email).first()
    if customer_by_id and customer_by_email and customer_by_id.id != customer_by_email.id:
        raise ValueError("Shopify customer ID and email belong to different customer records")

    customer = customer_by_id or customer_by_email
    if not customer:
        customer = Customer(email=email, shopify_customer_id=shopify_customer_id)
        db.session.add(customer)

    customer.email = email
    customer.shopify_customer_id = shopify_customer_id
    customer.name = f"{shopify_customer.get('first_name', '')} {shopify_customer.get('last_name', '')}".strip()
    db.session.flush()  # get customer.id without a full commit
    return customer


def upsert_order(shopify_order: dict, _image_cache: dict = None, *, commit=True) -> Order:
    from app.services import shopify_client
    from app.security import lock_login

    order_id = str(shopify_order["id"])
    lock_login("shopify-order:" + order_id)
    order = Order.query.filter_by(shopify_order_id=order_id).with_for_update().first()
    incoming_at = _utc(shopify_order["updated_at"]) if shopify_order.get("updated_at") else None
    if order and order.shopify_updated_at and (not incoming_at or incoming_at <= order.shopify_updated_at):
        if commit:
            db.session.commit()
        return order

    if _image_cache is None:
        _image_cache = {}

    shopify_customer = shopify_order.get("customer")

    if not shopify_customer or not shopify_customer.get("email"):
        return None
    customer = upsert_customer(shopify_customer)

    if not order:
        order = Order(shopify_order_id=str(shopify_order["id"]), customer_id=customer.id)
        db.session.add(order)
    order.shopify_updated_at = incoming_at

    order.order_number = str(shopify_order.get("order_number") or shopify_order.get("name", "")).lstrip("#")
    order.customer_id = customer.id
    order.payment_method = _parse_payment_method(shopify_order)
    order.financial_status = shopify_order.get("financial_status")
    if "shipping_address" in shopify_order:
        from app.services.address_service import normalize_address
        order.shipping_address = normalize_address(shopify_order.get("shipping_address") or {})

    fulfillments = shopify_order.get("fulfillments") or []
    if fulfillments:
        # Use the earliest fulfillment date as the return-window start.
        dates = [f["created_at"] for f in fulfillments if f.get("created_at")]
        if dates:
            order.fulfilled_at = _utc(min(dates))

    db.session.flush()

    mapping_pending = ExchangeRequest.query.join(OrderItem, ExchangeRequest.order_item_id == OrderItem.id).filter(
        OrderItem.order_id == order.id, ExchangeRequest.status == RequestStatus.COMPLETED,
        ExchangeRequest.replacement_line_id.is_(None)).count() > 0
    # A card represents one physical unit. Keep identifiers stable across repeat syncs.
    for line in shopify_order.get("line_items", []):
        line_id = str(line["id"])
        count = max(0, int(line.get("quantity", 1)))
        replacement_requests = ExchangeRequest.query.filter_by(replacement_line_id=line_id).all()
        if replacement_requests:
            # Replacement identity/delivery eligibility is owned by its exchange, not order resync.
            continue
        existing = {i.unit_number: i for i in OrderItem.query.filter_by(order_id=order.id, shopify_line_item_id=line_id).all()}
        dates = []
        for fulfillment in sorted(fulfillments, key=lambda f: f.get("created_at") or ""):
            if fulfillment.get("status") in ("cancelled", "failure", "error"):
                continue
            for fulfilled_line in fulfillment.get("line_items", []):
                if str(fulfilled_line.get("id")) == line_id and fulfillment.get("created_at"):
                    dates.extend([_utc(fulfillment["created_at"])] * int(fulfilled_line.get("quantity", 0)))
        refunded = sum(int(r.get("quantity", 0)) for refund in shopify_order.get("refunds", [])
                       for r in refund.get("refund_line_items", []) if str(r.get("line_item_id")) == line_id)
        for unit in range(1, count + 1):
            item = existing.get(unit)
            if item is None:
                item = OrderItem(order_id=order.id, shopify_line_item_id=line_id, unit_number=unit)
                db.session.add(item)
            item.shopify_variant_id = str(line.get("variant_id") or "")
            item.shopify_product_id = str(line.get("product_id") or "")
            item.product_title = line.get("title", "")
            item.size = next((o["value"] for o in line.get("properties", []) if o.get("name", "").lower() == "size"), line.get("variant_title"))
            item.sku = line.get("sku")
            item.quantity = 1
            item.line_quantity = count
            item.price = line.get("price", "0")
            item.fulfillment_known = True
            item.fulfilled_at = dates[unit-1] if unit <= len(dates) else None
            # Existing requests own their reserved units. External refunds are conservatively blocked.
            item.unavailable = bool(shopify_order.get("cancelled_at")) or refunded > 0 or (mapping_pending and (not existing or item.unavailable))
            product_id = item.shopify_product_id
            if product_id:
                if product_id not in _image_cache:
                    _image_cache[product_id] = shopify_client.get_product_image_url(product_id)
                item.image_url = _image_cache[product_id]
        for unit, item in existing.items():
            if unit > count:
                item.unavailable = True
    if "line_items" in shopify_order:
        line_ids = {str(line["id"]) for line in shopify_order["line_items"]}
        for item in order.items:
            if not item.replacement_for and item.shopify_line_item_id not in line_ids:
                item.unavailable = True

    if commit:
        db.session.commit()
    else:
        db.session.flush()
    return order


def sync_orders_for_customer(shopify_customer_id: str):
    """Full backfill for one customer -- used the first time a customer
    logs in, or via a manual admin 'resync' action."""
    from app.services import shopify_client

    orders = shopify_client.fetch_orders_for_customer(shopify_customer_id)
    image_cache = {}
    for o in orders:
        upsert_order(o, _image_cache=image_cache)
