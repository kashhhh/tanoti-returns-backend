from flask import Blueprint, request, jsonify, g

from app.extensions import db
from app.models import (
    Order, OrderItem, ReturnRequest, ExchangeRequest, AdminSettings,
    ReturnReason, ExchangeReason, RefundMode, RequestStatus, PaymentMethod,
)
from app.utils.decorators import login_required
from app.services import storage_service, shopify_client, sync_service
from app.services.address_service import pickup_from_form
from app.services.notification_service import queue_update, deliver_pending
from app.utils.numbering import next_return_number, next_exchange_number
from app.utils.tracking import build_timeline

customer_bp = Blueprint("customer", __name__)


def _item_payload(item: OrderItem, window_days: int, eligible: bool):
    active = item.active_request()
    return {
        "id": item.id,
        "product_title": item.product_title,
        "size": item.size,
        "price": str(item.price),
        "image_url": item.image_url,
        "order_number": item.order.order_number,
        "eligible": eligible and active is None,
        "pending": active is not None,
        "status": active.status.value if active else None,
    }


@customer_bp.route("/orders", methods=["GET"])
@login_required
def list_orders():
    """Returns two buckets: eligible-for-return items, and expired-window
    items, across all of the logged-in customer's orders. Items (not
    orders) are the clickable unit, per spec."""
    settings = AdminSettings.get()
    window_days = settings.return_window_days

    orders = Order.query.filter_by(customer_id=g.customer.id).all()

    eligible, expired = [], []
    for order in orders:
        within_window = order.is_within_return_window(window_days)
        for item in order.items:
            payload = _item_payload(item, window_days, within_window)
            (eligible if within_window else expired).append(payload)

    return jsonify({"eligible": eligible, "expired": expired})


@customer_bp.route("/orders/search", methods=["GET"])
@login_required
def search_order():
    """Order-ID search, scoped to the logged-in customer's own orders only."""
    query = request.args.get("order_number", "").strip()
    if not query:
        return jsonify({"error": "order_number is required"}), 400

    order = Order.query.filter_by(customer_id=g.customer.id, order_number=query).first()
    if not order:
        return jsonify({"error": "Order not found"}), 404

    settings = AdminSettings.get()
    within_window = order.is_within_return_window(settings.return_window_days)
    items = [_item_payload(i, settings.return_window_days, within_window) for i in order.items]
    return jsonify({"order_number": order.order_number, "items": items})


@customer_bp.route("/my-requests", methods=["GET"])
@login_required
def my_requests():
    """All of the customer's returns/exchanges, pending and completed --
    the dedicated status page requested."""
    returns = ReturnRequest.query.filter_by(customer_id=g.customer.id).order_by(
        ReturnRequest.created_at.desc()
    ).all()
    exchanges = ExchangeRequest.query.filter_by(customer_id=g.customer.id).order_by(
        ExchangeRequest.created_at.desc()
    ).all()

    def r_payload(r):
        return {
            "type": "return",
            "number": r.return_number,
            "item": r.order_item.product_title,
            "image_url": r.order_item.image_url,
            "size": r.order_item.size,
            "status": r.status.value,
            "created_at": r.created_at.isoformat(),
            "refund_mode": r.refund_mode.value,
            "net_refund_amount": str(r.net_refund_amount),
            "gift_card_code": r.gift_card_code,
            "rejection_reason": r.rejection_reason.value if r.rejection_reason else None,
            "rejection_note": r.rejection_note,
            "timeline": build_timeline(r, "return"),
        }

    def e_payload(e):
        return {
            "type": "exchange",
            "number": e.exchange_number,
            "item": e.order_item.product_title,
            "image_url": e.order_item.image_url,
            "size": e.order_item.size,
            "requested_size": e.requested_size,
            "status": e.status.value,
            "created_at": e.created_at.isoformat(),
            "rejection_reason": e.rejection_reason.value if e.rejection_reason else None,
            "rejection_note": e.rejection_note,
            "timeline": build_timeline(e, "exchange"),
        }

    combined = [r_payload(r) for r in returns] + [e_payload(e) for e in exchanges]
    combined.sort(key=lambda x: x["created_at"], reverse=True)
    return jsonify({"requests": combined})


@customer_bp.route("/order-items/<int:item_id>", methods=["GET"])
@login_required
def get_order_item(item_id):
    """The 'click a product' detail view: product info + billing info,
    used to render the exchange/return screen."""
    item = OrderItem.query.get_or_404(item_id)
    if item.order.customer_id != g.customer.id:
        return jsonify({"error": "Not found"}), 404

    settings = AdminSettings.get()

    variants = []
    if item.shopify_product_id:
        variants = shopify_client.get_variants_for_product(item.shopify_product_id)
    # Only in-stock sizes are selectable -- out-of-stock ones are blocked
    # from the exchange dropdown per the earlier decision.
    available_sizes = [v["size"] for v in variants if v["in_stock"]]

    return jsonify({
        "id": item.id,
        "product_title": item.product_title,
        "size": item.size,
        "price": str(item.price),
        "image_url": item.image_url,
        "order_number": item.order.order_number,
        "payment_method": item.order.payment_method.value,
        "shipping_address": item.order.shipping_address or {},
        "deduction_enabled": settings.deduction_enabled,
        "deduction_amount": str(settings.deduction_amount) if settings.deduction_enabled else "0",
        "available_sizes": available_sizes,
    })


@customer_bp.route("/resync", methods=["POST"])
@login_required
def resync_my_orders():
    """Manual 'refresh my orders' action -- pulls the customer's full
    order history from Shopify again. Useful right after a first login,
    or if a webhook was missed."""
    sync_service.sync_orders_for_customer(g.customer.shopify_customer_id)
    return jsonify({"message": "Orders synced"})


@customer_bp.route("/order-items/<int:item_id>/exchange", methods=["POST"])
@login_required
def submit_exchange(item_id):
    item = OrderItem.query.get_or_404(item_id)
    if item.order.customer_id != g.customer.id:
        return jsonify({"error": "Not found"}), 404
    if item.active_request() is not None:
        return jsonify({"error": "This item already has an active request"}), 400

    data = request.form
    size = data.get("size")
    reason = data.get("reason")
    reason_other = data.get("reason_other_text")

    if not size or reason not in ExchangeReason._value2member_map_:
        return jsonify({"error": "size and a valid reason are required"}), 400
    if reason == ExchangeReason.OTHER.value and not reason_other:
        return jsonify({"error": "reason_other_text is required when reason is 'other'"}), 400

    try:
        pickup_address = pickup_from_form(request.form, item.order)
        photos = storage_service.required_photos(request.files)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    number = next_exchange_number()
    try:
        photo_urls = storage_service.save_request_photos(photos, number)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    exchange = ExchangeRequest(
        order_item=item,
        customer=g.customer,
        exchange_number=number,
        photo_urls=photo_urls,
        pickup_address=pickup_address,
        order_item_id=item.id,
        customer_id=g.customer.id,
        requested_size=size,
        reason=ExchangeReason(reason),
        reason_other_text=reason_other,
    )
    db.session.add(exchange)
    queue_update(exchange, "exchange", "received")
    db.session.commit()
    deliver_pending(limit=1)
    return jsonify({"exchange_number": exchange.exchange_number, "status": exchange.status.value}), 201


@customer_bp.route("/order-items/<int:item_id>/return", methods=["POST"])
@login_required
def submit_return(item_id):
    """multipart/form-data: reason, reason_other_text, refund_mode, photo_front, photo_back, pickup_address"""
    item = OrderItem.query.get_or_404(item_id)
    if item.order.customer_id != g.customer.id:
        return jsonify({"error": "Not found"}), 404
    if item.active_request() is not None:
        return jsonify({"error": "This item already has an active request"}), 400

    reason = request.form.get("reason")
    reason_other = request.form.get("reason_other_text")
    refund_mode = request.form.get("refund_mode")
    try:
        pickup_address = pickup_from_form(request.form, item.order)
        photos = storage_service.required_photos(request.files)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    if reason not in ReturnReason._value2member_map_:
        return jsonify({"error": "A valid reason is required"}), 400
    if reason == ReturnReason.OTHER.value and not reason_other:
        return jsonify({"error": "reason_other_text is required when reason is 'other'"}), 400
    if refund_mode not in RefundMode._value2member_map_:
        return jsonify({"error": "A valid refund_mode is required"}), 400
    # COD orders can only refund to gift card -- decided requirement
    if item.order.payment_method == PaymentMethod.COD and refund_mode != RefundMode.GIFT_CARD.value:
        return jsonify({"error": "COD orders can only be refunded via gift card"}), 400
    if len(photos) < 2:
        return jsonify({"error": "At least 2 photos (front and back) are required"}), 400

    return_number = next_return_number()

    try:
        photo_urls = storage_service.save_request_photos(photos, return_number)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    settings = AdminSettings.get()
    deduction = settings.deduction_amount if settings.deduction_enabled else 0
    deduction = min(deduction, item.price)
    net_amount = item.price - deduction

    r = ReturnRequest(
        order_item=item,
        customer=g.customer,
        pickup_address=pickup_address,
        return_number=return_number,
        order_item_id=item.id,
        customer_id=g.customer.id,
        reason=ReturnReason(reason),
        reason_other_text=reason_other,
        photo_urls=photo_urls,
        refund_mode=RefundMode(refund_mode),
        refund_amount=item.price,
        deduction_applied=deduction,
        net_refund_amount=net_amount,
    )
    db.session.add(r)
    queue_update(r, "return", "received")
    db.session.commit()
    deliver_pending(limit=1)

    return jsonify({
        "return_number": r.return_number,
        "status": r.status.value,
        "net_refund_amount": str(r.net_refund_amount),
        "deduction_applied": str(r.deduction_applied),
    }), 201
