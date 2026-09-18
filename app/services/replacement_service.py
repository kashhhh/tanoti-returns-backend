"""A delivered replacement is a new physical unit, with its own request history."""
from app.extensions import db
from app.models import OrderItem


def ensure_replacement(req):
    """Reuse a Shopify-synced row when possible; never create two cards for one unit."""
    existing = OrderItem.query.filter_by(replacement_for=req.exchange_number).first()
    if existing:
        if req.delivered_at:
            existing.fulfilled_at = req.delivered_at
            existing.fulfillment_known = True
            existing.unavailable = False
        return existing
    if not req.delivered_at and not req.replacement_line_id:
        return None
    original = req.order_item
    # An orders/updated webhook may arrive before the returnProcess response.
    candidate = None
    if req.replacement_line_id:
        candidate = OrderItem.query.filter_by(order_id=original.order_id,
            shopify_line_item_id=req.replacement_line_id, replacement_for=None,
            request_claimed=False).order_by(OrderItem.unit_number).first()
    item = candidate or OrderItem(order_id=original.order_id, quantity=1, unit_number=1, line_quantity=1)
    item.shopify_line_item_id = req.replacement_line_id or "replacement:" + req.exchange_number
    item.shopify_variant_id = req.requested_variant_id
    item.shopify_product_id = original.shopify_product_id
    item.product_title = original.product_title
    item.size = req.requested_size
    item.price = original.price
    item.image_url = original.image_url
    item.replacement_for = req.exchange_number
    item.fulfilled_at = req.delivered_at
    item.fulfillment_known = True
    item.unavailable = req.delivered_at is None
    db.session.add(item)
    return item
