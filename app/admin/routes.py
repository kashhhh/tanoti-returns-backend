from datetime import datetime

from flask import Blueprint, request, jsonify

from app.extensions import db
from app.models import (
    ReturnRequest, ExchangeRequest, AdminSettings, RequestStatus,
    RejectionReason, RefundMode,
)
from app.utils.admin_auth import admin_required
from app.services import shopify_client, email_service, shipping_service

admin_bp = Blueprint("admin", __name__)


def _repeat_returner_flag(customer_id: int) -> bool:
    from datetime import timedelta
    cutoff = datetime.utcnow() - timedelta(days=90)
    r_count = ReturnRequest.query.filter(
        ReturnRequest.customer_id == customer_id, ReturnRequest.created_at >= cutoff
    ).count()
    e_count = ExchangeRequest.query.filter(
        ExchangeRequest.customer_id == customer_id, ExchangeRequest.created_at >= cutoff
    ).count()
    return (r_count + e_count) >= 3


def _row(obj, kind: str) -> dict:
    row = {
        "type": kind,
        "number": obj.return_number if kind == "return" else obj.exchange_number,
        "date": obj.created_at.isoformat(),
        "item": obj.order_item.product_title,
        "image_url": obj.order_item.image_url,
        "size": obj.order_item.size,
        "order_number": obj.order_item.order.order_number,
        "status": obj.status.value,
        "rejected_stage": obj.rejected_stage,
        "rejection_reason": obj.rejection_reason.value if obj.rejection_reason else None,
        "rejection_note": obj.rejection_note,
        "reason": obj.reason.value,
        "reason_other_text": obj.reason_other_text,
        "customer_email": obj.customer.email,
        "repeat_returner": _repeat_returner_flag(obj.customer_id),
        "pickup_carrier": obj.pickup_carrier,
        "pickup_tracking_id": obj.pickup_tracking_id,
        "pickup_status": obj.pickup_status,
        "pickup_tracking_url": shipping_service.tracking_url(obj.pickup_carrier, obj.pickup_tracking_id),
        "parcel_received_at": obj.parcel_received_at.isoformat() if obj.parcel_received_at else None,
        "completed_at": obj.completed_at.isoformat() if obj.completed_at else None,
    }
    if kind == "return":
        row["photo_urls"] = obj.photo_urls or []
        row["refund_mode"] = obj.refund_mode.value
        row["net_refund_amount"] = str(obj.net_refund_amount)
        row["gift_card_code"] = obj.gift_card_code
    else:
        row["requested_size"] = obj.requested_size
        row["outbound_carrier"] = obj.outbound_carrier
        row["outbound_tracking_id"] = obj.outbound_tracking_id
        row["outbound_status"] = obj.outbound_status
        row["outbound_tracking_url"] = shipping_service.tracking_url(obj.outbound_carrier, obj.outbound_tracking_id)
    return row


@admin_bp.route("/requests", methods=["GET"])
@admin_required
def list_requests():
    returns = ReturnRequest.query.order_by(ReturnRequest.created_at.desc()).all()
    exchanges = ExchangeRequest.query.order_by(ExchangeRequest.created_at.desc()).all()

    rows = [_row(r, "return") for r in returns] + [_row(e, "exchange") for e in exchanges]
    rows.sort(key=lambda x: x["date"], reverse=True)
    return jsonify({"requests": rows})


def _find_request(kind: str, number: str):
    if kind == "return":
        return ReturnRequest.query.filter_by(return_number=number).first()
    return ExchangeRequest.query.filter_by(exchange_number=number).first()


def _require_status(req, expected: RequestStatus):
    if req.status != expected:
        return jsonify({"error": f"Request is {req.status.value}, expected {expected.value}"}), 400
    return None


@admin_bp.route("/requests/<kind>/<number>/accept-photos", methods=["POST"])
@admin_required
def accept_photos(kind, number):
    req = _find_request(kind, number)
    if not req:
        return jsonify({"error": "Not found"}), 404
    err = _require_status(req, RequestStatus.PENDING)
    if err:
        return err

    req.photo_decision_at = datetime.utcnow()

    try:
        carrier, tracking_id, status = shipping_service.schedule_reverse_pickup(
            order=req.order_item.order, item=req.order_item, request_number=number
        )
        req.pickup_carrier = carrier
        req.pickup_tracking_id = tracking_id
        req.pickup_status = status
    except Exception:
        req.pickup_carrier = "manual"
        req.pickup_status = "manual_scheduling_required"

    req.status = RequestStatus.PICKUP_SCHEDULED
    db.session.commit()

    email_service.send_status_update_email(req.customer.email, number, req.status.value)
    return jsonify(_row(req, kind))


@admin_bp.route("/requests/<kind>/<number>/reject-photos", methods=["POST"])
@admin_required
def reject_photos(kind, number):
    req = _find_request(kind, number)
    if not req:
        return jsonify({"error": "Not found"}), 404
    err = _require_status(req, RequestStatus.PENDING)
    if err:
        return err
    return _reject(req, kind, number, stage="photo")


@admin_bp.route("/requests/<kind>/<number>/mark-parcel-received", methods=["POST"])
@admin_required
def mark_parcel_received(kind, number):
    req = _find_request(kind, number)
    if not req:
        return jsonify({"error": "Not found"}), 404
    err = _require_status(req, RequestStatus.PICKUP_SCHEDULED)
    if err:
        return err

    req.parcel_received_at = datetime.utcnow()
    req.status = RequestStatus.PARCEL_RECEIVED
    db.session.commit()

    email_service.send_status_update_email(req.customer.email, number, req.status.value)
    return jsonify(_row(req, kind))


@admin_bp.route("/requests/<kind>/<number>/accept-parcel", methods=["POST"])
@admin_required
def accept_parcel(kind, number):
    req = _find_request(kind, number)
    if not req:
        return jsonify({"error": "Not found"}), 404
    err = _require_status(req, RequestStatus.PARCEL_RECEIVED)
    if err:
        return err

    req.parcel_decision_at = datetime.utcnow()

    if kind == "return":
        if req.refund_mode == RefundMode.GIFT_CARD:
            try:
                gift_card = shopify_client.issue_gift_card(
                    amount=str(req.net_refund_amount), note=f"Refund for {req.return_number}"
                )
                req.gift_card_code = gift_card.get("code")
            except Exception as e:
                db.session.commit()
                return jsonify({"error": f"Parcel accepted, but gift card issuance failed: {e}"}), 502
        req.status = RequestStatus.COMPLETED
        req.completed_at = datetime.utcnow()

    else:  # exchange: ship the replacement item
        try:
            carrier, tracking_id, status = shipping_service.schedule_forward_shipment(
                order=req.order_item.order,
                item=req.order_item,
                requested_size=req.requested_size,
                request_number=number,
            )
            req.outbound_carrier = carrier
            req.outbound_tracking_id = tracking_id
            req.outbound_status = status
        except Exception:
            req.outbound_carrier = "manual"
            req.outbound_status = "manual_scheduling_required"
        req.status = RequestStatus.COMPLETED
        req.completed_at = datetime.utcnow()

    db.session.commit()
    email_service.send_status_update_email(req.customer.email, number, req.status.value)
    return jsonify(_row(req, kind))


@admin_bp.route("/requests/<kind>/<number>/reject-parcel", methods=["POST"])
@admin_required
def reject_parcel(kind, number):
    req = _find_request(kind, number)
    if not req:
        return jsonify({"error": "Not found"}), 404
    err = _require_status(req, RequestStatus.PARCEL_RECEIVED)
    if err:
        return err
    return _reject(req, kind, number, stage="parcel")


def _reject(req, kind, number, stage: str):
    data = request.json or {}
    reason = data.get("rejection_reason")
    note = data.get("note", "")
    if reason not in RejectionReason._value2member_map_:
        return jsonify({"error": "A valid rejection_reason is required"}), 400

    req.status = RequestStatus.REJECTED
    req.rejected_stage = stage
    req.rejection_reason = RejectionReason(reason)
    req.rejection_note = note
    if stage == "photo":
        req.photo_decision_at = datetime.utcnow()
    else:
        req.parcel_decision_at = datetime.utcnow()
    db.session.commit()

    email_service.send_rejection_email(
        to_email=req.customer.email,
        item_title=req.order_item.product_title,
        request_number=number,
        rejection_reason=req.rejection_reason.value.replace("_", " "),
        note=note,
    )
    return jsonify(_row(req, kind))


@admin_bp.route("/analytics/overview", methods=["GET"])
@admin_required
def analytics_overview():
    from sqlalchemy import func
    from app.models import OrderItem

    top_returned_products = (
        db.session.query(
            OrderItem.product_title, func.count(ReturnRequest.id).label("count")
        )
        .join(ReturnRequest, ReturnRequest.order_item_id == OrderItem.id)
        .group_by(OrderItem.product_title)
        .order_by(func.count(ReturnRequest.id).desc())
        .limit(10)
        .all()
    )

    reason_breakdown = (
        db.session.query(ReturnRequest.reason, func.count(ReturnRequest.id))
        .group_by(ReturnRequest.reason)
        .all()
    )

    returns_by_size = (
        db.session.query(OrderItem.size, func.count(ReturnRequest.id))
        .join(ReturnRequest, ReturnRequest.order_item_id == OrderItem.id)
        .group_by(OrderItem.size)
        .all()
    )

    exchange_reason_breakdown = (
        db.session.query(ExchangeRequest.reason, func.count(ExchangeRequest.id))
        .group_by(ExchangeRequest.reason)
        .all()
    )

    return jsonify({
        "top_returned_products": [{"product": p, "count": c} for p, c in top_returned_products],
        "return_reason_breakdown": [{"reason": r.value, "count": c} for r, c in reason_breakdown],
        "returns_by_size": [{"size": s, "count": c} for s, c in returns_by_size],
        "exchange_reason_breakdown": [{"reason": r.value, "count": c} for r, c in exchange_reason_breakdown],
    })


@admin_bp.route("/settings", methods=["GET"])
@admin_required
def get_settings():
    s = AdminSettings.get()
    return jsonify({
        "return_window_days": s.return_window_days,
        "deduction_enabled": s.deduction_enabled,
        "deduction_amount": str(s.deduction_amount),
    })


@admin_bp.route("/settings", methods=["PUT"])
@admin_required
def update_settings():
    s = AdminSettings.get()
    data = request.json or {}

    if "return_window_days" in data:
        s.return_window_days = int(data["return_window_days"])
    if "deduction_enabled" in data:
        s.deduction_enabled = bool(data["deduction_enabled"])
    if "deduction_amount" in data:
        s.deduction_amount = data["deduction_amount"]

    db.session.commit()
    return jsonify({"message": "Settings updated"})