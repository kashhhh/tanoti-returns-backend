"""
Turns a Shopify order payload (from a webhook or a REST pull) into local
Customer / Order / OrderItem rows. Idempotent: re-running with the same
order just updates the existing rows rather than duplicating them.
"""
from datetime import datetime

from app.extensions import db
from app.models import Customer, Order, OrderItem, PaymentMethod




def _parse_payment_method(shopify_order: dict) -> PaymentMethod:
    gateways = [g.lower() for g in shopify_order.get("payment_gateway_names", [])]
    if any("cash on delivery" in g or g == "cod" for g in gateways):
        return PaymentMethod.COD
    return PaymentMethod.PREPAID


def upsert_customer(shopify_customer: dict) -> Customer:
    email = (shopify_customer.get("email") or "").strip().lower()
    customer = Customer.query.filter_by(email=email).first()
    if not customer:
        customer = Customer(email=email)
        db.session.add(customer)

    customer.shopify_customer_id = str(shopify_customer["id"])
    customer.name = f"{shopify_customer.get('first_name', '')} {shopify_customer.get('last_name', '')}".strip()
    db.session.flush()  # get customer.id without a full commit
    return customer


def upsert_order(shopify_order: dict, _image_cache: dict = None) -> Order:
    from app.services import shopify_client

    if _image_cache is None:
        _image_cache = {}

    shopify_customer = shopify_order.get("customer")

    customer = upsert_customer(shopify_customer)

    order = Order.query.filter_by(shopify_order_id=str(shopify_order["id"])).first()
    if not order:
        order = Order(shopify_order_id=str(shopify_order["id"]), customer_id=customer.id)
        db.session.add(order)

    order.order_number = str(shopify_order.get("order_number") or shopify_order.get("name", "")).lstrip("#")
    order.customer_id = customer.id
    order.payment_method = _parse_payment_method(shopify_order)
    order.financial_status = shopify_order.get("financial_status")

    fulfillments = shopify_order.get("fulfillments") or []
    if fulfillments:
        # Use the earliest fulfillment date as the return-window start.
        dates = [f["created_at"] for f in fulfillments if f.get("created_at")]
        if dates:
            order.fulfilled_at = datetime.fromisoformat(min(dates).replace("Z", "+00:00")).replace(tzinfo=None)

    db.session.flush()

    # Line items
    existing_line_ids = {str(li_id) for (li_id,) in db.session.query(OrderItem.shopify_line_item_id).filter_by(order_id=order.id)}
    for line_item in shopify_order.get("line_items", []):
        li_id = str(line_item["id"])
        item = OrderItem.query.filter_by(order_id=order.id, shopify_line_item_id=li_id).first()
        if not item:
            item = OrderItem(order_id=order.id, shopify_line_item_id=li_id)
            db.session.add(item)

        item.shopify_variant_id = str(line_item.get("variant_id") or "")
        item.shopify_product_id = str(line_item.get("product_id") or "")
        item.product_title = line_item.get("title", "")
        item.size = next(
            (o["value"] for o in line_item.get("properties", []) if o.get("name", "").lower() == "size"),
            line_item.get("variant_title"),
        )
        item.sku = line_item.get("sku")
        item.quantity = line_item.get("quantity", 1)
        item.price = line_item.get("price", "0")
    
        # Order line items don't carry image data -- fetch it from the
        # product separately, cached per product ID within this sync run
        # so the same product isn't fetched once per line item/order.
        product_id = item.shopify_product_id
        if product_id:
            if product_id not in _image_cache:
                _image_cache[product_id] = shopify_client.get_product_image_url(product_id)
            item.image_url = _image_cache[product_id]

        existing_line_ids.discard(li_id)

    db.session.commit()
    return order


def sync_orders_for_customer(shopify_customer_id: str):
    """Full backfill for one customer -- used the first time a customer
    logs in, or via a manual admin 'resync' action."""
    from app.services import shopify_client

    orders = shopify_client.fetch_orders_for_customer(shopify_customer_id)
    image_cache = {}
    for o in orders:
        upsert_order(o, _image_cache=image_cache)
