from datetime import datetime

from flask import Blueprint, request, jsonify, g, current_app

from app.extensions import db
from app.models import (
    ReturnRequest, ExchangeRequest, AdminSettings, RequestStatus,
    RejectionReason, RefundMode, GiftCardIssuance,
)
from app.utils.admin_auth import admin_required
from app.services import shopify_client, shipping_service
from app.services.notification_service import queue_update, deliver_pending

from app.utils.request_status import stage_for
from app.services.shopify_returns import sync_one
from app.services.request_listing import list_page

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


def _row(obj, kind: str, repeat_flags=None) -> dict:
    row = {
        "type": kind,
        "number": obj.return_number if kind == "return" else obj.exchange_number,
        "date": obj.created_at.isoformat(),
        "item": obj.order_item.product_title,
        "image_url": obj.order_item.image_url,
        "size": obj.order_item.size,
        "order_number": obj.order_item.order.order_number,
        "status": obj.status.value,
        "stage": stage_for(obj, kind),
        "rejected_stage": obj.rejected_stage,
        "rejection_reason": obj.rejection_reason.value if obj.rejection_reason else None,
        "rejection_note": obj.rejection_note,
        "reason": obj.reason.value,
        "reason_other_text": obj.reason_other_text,
        "customer_note": obj.customer_note,
        "customer_email": obj.customer.email,
        "replacement_for": obj.order_item.replacement_for,
        "unit_number": obj.order_item.unit_number,
        "shopify_sync_status": obj.shopify_sync_status,
        "shopify_sync_error": obj.shopify_sync_error,
        "shopify_return_id": obj.shopify_return_id,
        "restock": obj.restock,
        "pickup_address": obj.pickup_address or obj.order_item.order.shipping_address or {},
        "shipping_address": obj.order_item.order.shipping_address or {},
        "photo_urls": obj.photo_urls or [],
        "repeat_returner": repeat_flags.get(obj.customer_id, False) if repeat_flags is not None else _repeat_returner_flag(obj.customer_id),
        "pickup_carrier": obj.pickup_carrier,
        "pickup_tracking_id": obj.pickup_tracking_id,
        "pickup_status": obj.pickup_status,
        "pickup_tracking_url": shipping_service.tracking_url(obj.pickup_carrier, obj.pickup_tracking_id),
        "shipping_bookings": shipping_service.booking_details(obj.return_number if kind == "return" else obj.exchange_number),
        "parcel_received_at": obj.parcel_received_at.isoformat() if obj.parcel_received_at else None,
        "completed_at": obj.completed_at.isoformat() if obj.completed_at else None,
    }
    if kind == "return":
        row["photo_urls"] = obj.photo_urls or []
        row["refund_mode"] = obj.refund_mode.value
        row["net_refund_amount"] = str(obj.net_refund_amount)
        row["gift_card_code"] = obj.gift_card_code
        issuance = db.session.get(GiftCardIssuance, obj.id)
        row["gift_card_issuance_state"] = issuance.state if issuance else ("legacy_review" if obj.refund_mode == RefundMode.GIFT_CARD and obj.parcel_decision_at and not obj.gift_card_code else None)
        row["gift_card_provider_id"] = issuance.provider_id if issuance else None
        row["refund_paid_at"] = obj.refund_paid_at.isoformat() if obj.refund_paid_at else None
        row["refund_paid_by"] = obj.refund_paid_by
    else:
        row["requested_size"] = obj.requested_size
        row["delivered_at"] = obj.delivered_at.isoformat() if obj.delivered_at else None
        row["delivered_by"] = obj.delivered_by
        row["outbound_carrier"] = obj.outbound_carrier
        row["outbound_tracking_id"] = obj.outbound_tracking_id
        row["outbound_status"] = obj.outbound_status
        row["outbound_tracking_url"] = shipping_service.tracking_url(obj.outbound_carrier, obj.outbound_tracking_id)
    return row


@admin_bp.route("/requests", methods=["GET"])
@admin_required
def list_requests():
    try:
        objects, pagination, repeat_flags = list_page(request.args)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"requests": [_row(obj, kind, repeat_flags) for obj, kind in objects], **pagination})


def _find_request(kind: str, number: str):
    if kind == "return":
        return ReturnRequest.query.filter_by(return_number=number).with_for_update().first()
    if kind == "exchange":
        return ExchangeRequest.query.filter_by(exchange_number=number).with_for_update().first()
    return None


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
    # Save approval with the booking fence before any remote call can release the lock.
    req.status = RequestStatus.PICKUP_SCHEDULED

    try:
        carrier, tracking_id, status = shipping_service.schedule_reverse_pickup(
            order=req.order_item.order, item=req.order_item, request_number=number,
            pickup_address=req.pickup_address
        )
        req.pickup_carrier = carrier
        req.pickup_tracking_id = tracking_id
        req.pickup_status = status
    except Exception:
        req.pickup_carrier = "manual"
        req.pickup_status = "manual_scheduling_required"

    req.status = RequestStatus.PICKUP_SCHEDULED
    queue_update(req, kind, "pickup_booked" if req.pickup_tracking_id else "pickup_pending")
    db.session.commit()
    sync_one(req, kind)
    deliver_pending(limit=1)
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
    queue_update(req, kind, "parcel_received")
    db.session.commit()
    sync_one(req, kind)
    deliver_pending(limit=1)
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

    choice = (request.get_json(silent=True) or {}).get("restock", False)
    if not isinstance(choice, bool):
        return jsonify({"error": "Choose whether the inspected item should be restocked"}), 400
    if kind == "return" and req.refund_mode == RefundMode.GIFT_CARD:
        attempt = db.session.get(GiftCardIssuance, req.id)
        if (attempt and attempt.state != "rejected") or req.gift_card_code or req.parcel_decision_at:
            return jsonify({"error": "A gift card may already exist. Reconcile the existing issuance before proceeding."}), 409
    req.restock = choice
    if kind == "exchange":
        try:
            variant = shopify_client.select_exchange_variant(req.order_item, req.requested_size)
            if req.requested_variant_id and variant["id"] != req.requested_variant_id:
                return jsonify({"error": "Replacement variant changed; review the request"}), 409
            req.requested_variant_id = variant["id"]
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception:
            return jsonify({"error": "Could not verify replacement stock. Please retry."}), 503
    req.parcel_decision_at = datetime.utcnow()

    if kind == "return":
        if req.refund_mode == RefundMode.GIFT_CARD:
            from app.services.gift_card_service import issue_once, IssuanceUncertain
            try:
                issue_once(req)
            except IssuanceUncertain as exc:
                return jsonify({"error": str(exc)}), 409
            except ValueError as exc:
                db.session.rollback()
                return jsonify({"error": str(exc)}), 400
        req.status = RequestStatus.COMPLETED
        req.completed_at = datetime.utcnow() if req.refund_mode == RefundMode.GIFT_CARD else None

    else:  # exchange: ship the replacement item
        req.status = RequestStatus.COMPLETED
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
        req.completed_at = None

    if kind == "return":
        event = "gift_card" if req.refund_mode == RefundMode.GIFT_CARD else "refund_approved"
    else:
        event = "replacement_booked" if req.outbound_tracking_id else "replacement_pending"
    queue_update(req, kind, event)
    db.session.commit()
    sync_one(req, kind)
    deliver_pending(limit=1)
    return jsonify(_row(req, kind))


@admin_bp.post("/requests/return/<number>/reconcile-gift-card")
@admin_required
def reconcile_gift_card(number):
    import re
    from app.services.gift_card_service import confirm
    req = _find_request("return", number)
    if not req:
        return jsonify({"error": "Not found"}), 404
    attempt = db.session.get(GiftCardIssuance, req.id)
    if not attempt:
        return jsonify({"error": "No recoverable issuance record exists. Manual investigation is required."}), 409
    if attempt.state == "confirmed":
        return jsonify(_row(req, "return"))
    if req.status != RequestStatus.PARCEL_RECEIVED:
        return jsonify({"error": "Refund is not awaiting reconciliation"}), 409
    ident = (request.get_json(silent=True) or {}).get("gift_card_id", "")
    if not isinstance(ident, str) or not re.fullmatch(r"[1-9][0-9]{0,19}", ident):
        return jsonify({"error": "Enter the numeric Shopify gift card ID"}), 400
    try:
        card = shopify_client.fetch_gift_card(ident)
        if str(card.get("id")) != ident:
            raise ValueError("Shopify returned a different gift card")
        confirm(attempt, req, card)
    except ValueError as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 409
    except Exception:
        db.session.rollback()
        return jsonify({"error": "Could not verify the existing gift card with Shopify. No new card was created."}), 503
    req.status = RequestStatus.COMPLETED
    req.completed_at = datetime.utcnow()
    queue_update(req, "return", "gift_card")
    db.session.commit()
    sync_one(req, "return")
    deliver_pending(limit=1)
    return jsonify(_row(req, "return"))


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
    attempt = db.session.get(GiftCardIssuance, req.id) if kind == "return" else None
    if kind == "return" and ((attempt and attempt.state != "rejected") or req.gift_card_code
                              or (req.refund_mode == RefundMode.GIFT_CARD and req.parcel_decision_at)):
        return jsonify({"error": "A gift card may already exist. Reconcile it before changing this refund."}), 409
    data = request.json or {}
    reason = data.get("rejection_reason")
    note = data.get("note", "")
    if not isinstance(reason, str) or reason not in RejectionReason._value2member_map_:
        return jsonify({"error": "A valid rejection_reason is required"}), 400

    if not isinstance(note, str) or len(note) > 2000:
        return jsonify({"error": "Rejection note must be at most 2,000 characters"}), 400
    req.status = RequestStatus.REJECTED
    req.rejected_stage = stage
    req.rejection_reason = RejectionReason(reason)
    req.rejection_note = note
    if stage == "photo":
        req.photo_decision_at = datetime.utcnow()
    else:
        req.parcel_decision_at = datetime.utcnow()
    queue_update(req, kind, "rejected")
    db.session.commit()
    sync_one(req, kind)
    deliver_pending(limit=1)
    return jsonify(_row(req, kind))


@admin_bp.post("/requests/return/<number>/mark-refund-paid")
@admin_required
def mark_refund_paid(number):
    req = ReturnRequest.query.filter_by(return_number=number).with_for_update().first()
    if not req:
        return jsonify({"error": "Request not found"}), 404
    if req.status != RequestStatus.COMPLETED or req.refund_mode != RefundMode.ACCOUNT or req.gift_card_code:
        return jsonify({"error": "Only an approved manual refund can be marked paid"}), 400
    if req.refund_paid_at:
        return jsonify(_row(req, "return"))
    req.refund_paid_at = datetime.utcnow()
    req.refund_paid_by = g.admin_email
    req.completed_at = req.refund_paid_at
    queue_update(req, "return", "refund_paid")
    db.session.commit()
    sync_one(req, "return")
    deliver_pending(limit=1)
    return jsonify(_row(req, "return"))


@admin_bp.post("/requests/exchange/<number>/mark-delivered")
@admin_required
def mark_delivered(number):
    req = ExchangeRequest.query.filter_by(exchange_number=number).with_for_update().first()
    if not req:
        return jsonify({"error": "Request not found"}), 404
    if req.status != RequestStatus.COMPLETED:
        return jsonify({"error": "Only an approved exchange can be marked delivered"}), 400
    if req.delivered_at:
        return jsonify(_row(req, "exchange"))
    # A manual shipment can be confirmed delivered even without an API waybill.
    req.delivered_at = datetime.utcnow()
    req.delivered_by = g.admin_email
    req.completed_at = req.delivered_at
    req.outbound_status = "delivered"
    from app.services.replacement_service import ensure_replacement
    ensure_replacement(req)
    queue_update(req, "exchange", "delivered")
    db.session.commit()
    sync_one(req, "exchange")
    deliver_pending(limit=1)
    return jsonify(_row(req, "exchange"))


@admin_bp.post("/requests/<kind>/<number>/sync-shopify")
@admin_required
def sync_shopify(kind, number):
    if not current_app.config.get("SHOPIFY_RETURNS_SYNC_ENABLED"):
        return jsonify({"error": "Shopify return/restock sync is disabled. Order imports and gift cards remain active."}), 410
    req = _find_request(kind, number)
    if not req:
        return jsonify({"error": "Not found"}), 404
    # Optional restock decision lets legacy approved records be processed safely.
    data = request.get_json(silent=True) or {}
    if "restock" in data and req.restock is None:
        if not isinstance(data["restock"], bool):
            return jsonify({"error": "Restock must be true or false"}), 400
        req.restock = data["restock"]
    db.session.commit()
    sync_one(req, kind)
    return jsonify(_row(req, kind))


@admin_bp.post("/requests/<kind>/<number>/confirm-shipment")
@admin_required
def confirm_shipment(kind, number):
    """Record a manually booked shipment and send its milestone once."""
    req = _find_request(kind, number)
    if not req:
        return jsonify({"error": "Not found"}), 404
    data = request.get_json(silent=True) or {}
    leg = data.get("leg")
    outbound = leg == "replacement"
    if leg not in ("pickup", "replacement") or (outbound and kind != "exchange"):
        return jsonify({"error": "Choose a pickup or replacement shipment"}), 400
    expected = RequestStatus.COMPLETED if outbound else RequestStatus.PICKUP_SCHEDULED
    if req.status != expected or (outbound and req.delivered_at):
        return jsonify({"error": "This request is not awaiting this shipment"}), 409
    carrier, tracking = data.get("carrier"), data.get("tracking_id")
    import re
    if (not isinstance(carrier, str) or not 1 <= len(carrier.strip()) <= 32
            or not isinstance(tracking, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", tracking)):
        return jsonify({"error": "Enter a carrier and valid tracking number"}), 400
    prefix = "outbound" if outbound else "pickup"
    booking = shipping_service.booking_details(number).get(leg)
    if booking and booking["state"] != "not_attempted":
        return jsonify({"error": "An API booking already exists or may have succeeded. Use Verify Delhivery waybill instead."}), 409
    existing = getattr(req, prefix + "_tracking_id")
    if existing:
        if existing == tracking and getattr(req, prefix + "_carrier") == carrier.strip().lower():
            return jsonify(_row(req, kind))
        return jsonify({"error": "A shipment is already recorded for this request"}), 409
    setattr(req, prefix + "_carrier", carrier.strip().lower())
    setattr(req, prefix + "_tracking_id", tracking)
    setattr(req, prefix + "_status", "scheduled")
    queue_update(req, kind, "replacement_booked" if outbound else "pickup_booked")
    db.session.commit()
    deliver_pending(limit=1)
    return jsonify(_row(req, kind))


@admin_bp.post("/requests/<kind>/<number>/book-shipment")
@admin_bp.post("/requests/<kind>/<number>/reconcile-shipment")
@admin_required
def recover_shipment(kind, number):
    req = _find_request(kind, number)
    if not req:
        return jsonify({"error": "Not found"}), 404
    data = request.get_json(silent=True) or {}
    leg = data.get("leg")
    if leg not in ("pickup", "replacement") or (leg == "replacement" and kind != "exchange"):
        return jsonify({"error": "Invalid shipment leg"}), 400
    outbound = leg == "replacement"
    expected = RequestStatus.COMPLETED if outbound else RequestStatus.PICKUP_SCHEDULED
    if req.status != expected or (outbound and req.delivered_at):
        return jsonify({"error": "This request is not awaiting this shipment"}), 409
    booking = shipping_service.booking_details(number).get(leg)
    if not booking:
        return jsonify({"error": "No recoverable API attempt exists. Check earlier bookings in Delhivery One."}), 409
    prefix = "outbound" if outbound else "pickup"
    existing = getattr(req, prefix + "_tracking_id")
    if existing:
        return jsonify(_row(req, kind))
    try:
        if request.path.endswith("/reconcile-shipment"):
            carrier, waybill, status = shipping_service.reconcile(number, leg, data.get("waybill", ""))
        elif booking["state"] not in ("not_attempted", "confirmed"):
            return jsonify({"error": "The previous attempt is uncertain. Verify its waybill instead of retrying."}), 409
        elif outbound:
            variant = shopify_client.select_exchange_variant(req.order_item, req.requested_size)
            if variant["id"] != req.requested_variant_id:
                return jsonify({"error": "Replacement variant changed. Review the request before booking."}), 409
            carrier, waybill, status = shipping_service.schedule_forward_shipment(order=req.order_item.order,
                item=req.order_item, requested_size=req.requested_size, request_number=number)
        else:
            carrier, waybill, status = shipping_service.schedule_reverse_pickup(order=req.order_item.order,
                item=req.order_item, request_number=number, pickup_address=req.pickup_address)
    except ValueError as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 409 if isinstance(exc, shipping_service.BookingUncertain) else 400
    except Exception:
        db.session.rollback()
        return jsonify({"error": "Could not verify or book the shipment. Refresh the request before proceeding."}), 503
    req = _find_request(kind, number)
    setattr(req, prefix + "_carrier", carrier)
    setattr(req, prefix + "_tracking_id", waybill)
    setattr(req, prefix + "_status", status)
    queue_update(req, kind, "replacement_booked" if outbound else "pickup_booked")
    db.session.commit()
    deliver_pending(limit=1)
    return jsonify(_row(req, kind))


@admin_bp.post("/requests/<kind>/<number>/refresh-tracking")
@admin_required
def refresh_tracking(kind, number):
    req = _find_request(kind, number)
    if not req:
        return jsonify({"error": "Not found"}), 404
    try:
        shipping_service.refresh_tracking(req, kind)
    except Exception:
        db.session.rollback()
        return jsonify({"error": "Delhivery tracking is unavailable. Existing shipment details have been kept."}), 503
    db.session.commit()
    deliver_pending(limit=1)
    return jsonify(_row(req, kind))


@admin_bp.get("/analytics/overview")
@admin_required
def analytics_overview():
    from app.services.analytics_service import overview
    try:
        return jsonify(overview(request.args))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


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
    from decimal import Decimal, InvalidOperation
    s = AdminSettings.get()
    data = request.json or {}
    days = data.get("return_window_days", s.return_window_days)
    enabled = data.get("deduction_enabled", s.deduction_enabled)
    try:
        amount = Decimal(str(data.get("deduction_amount", s.deduction_amount)))
        if not amount.is_finite() or not 0 <= amount <= Decimal("99999999.99") or amount != amount.quantize(Decimal("0.01")):
            raise ValueError()
    except (InvalidOperation, ValueError):
        return jsonify({"error": "Deduction must be a nonnegative amount with at most two decimal places"}), 400
    if type(days) is not int or not 1 <= days <= 365 or type(enabled) is not bool:
        return jsonify({"error": "Return window must be 1–365 days and deduction_enabled must be a boolean"}), 400
    s.return_window_days = days
    s.deduction_enabled = enabled
    s.deduction_amount = amount
    from app.utils.audit import record_admin_action
    record_admin_action("settings_updated", "settings")
    db.session.commit()
    return jsonify({"message": "Settings updated"})


@admin_bp.get("/audit")
@admin_required
def audit_log():
    from app.models import AdminAudit
    try:
        page = int(request.args.get("page", "1"))
        if not 1 <= page <= 10000:
            raise ValueError()
    except ValueError:
        return jsonify({"error": "Invalid audit page"}), 400
    rows = AdminAudit.query.order_by(AdminAudit.id.desc()).offset((page - 1) * 50).limit(50).all()
    return jsonify({"page": page, "events": [{"id": row.id, "actor": row.actor, "action": row.action,
                      "target": row.target, "created_at": row.created_at.isoformat()} for row in rows]})
