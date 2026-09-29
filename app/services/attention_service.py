"""Shared SQL predicates for attention counts, filtering, and row explanations."""
from datetime import datetime, timedelta
from sqlalchemy import and_, or_, exists, select, func
from app.extensions import db
from app.models import ShippingBooking, Notification, GiftCardIssuance, RequestStatus


def rules(model, kind, now=None):
    now = now or datetime.utcnow()
    number = model.return_number if kind == "return" else model.exchange_number
    booking = lambda condition: exists(select(ShippingBooking.reference).where(
        ShippingBooking.request_number == number, condition)).correlate(model)
    terminal = (and_(model.status == RequestStatus.COMPLETED,
                     or_(model.gift_card_code.isnot(None), model.refund_paid_at.isnot(None), model.net_refund_amount == 0))
                if kind == "return" else model.delivered_at.isnot(None))
    active = and_(model.status != RequestStatus.REJECTED, ~terminal)
    checks = [
        ("Photo review overdue", and_(model.status == RequestStatus.PENDING, model.created_at < now - timedelta(hours=24))),
        ("Pickup overdue — check collection", and_(model.status == RequestStatus.PICKUP_SCHEDULED,
            model.photo_decision_at < now - timedelta(days=3),
            func.lower(func.coalesce(model.pickup_status, "")).in_(["scheduled", "manifested", "open", "pending", "manual_scheduling_required", ""]))),
        ("Inspection overdue", and_(model.status == RequestStatus.PARCEL_RECEIVED,
            model.parcel_received_at < now - timedelta(days=2))),
        ("Pickup booking missing", and_(model.status == RequestStatus.PICKUP_SCHEDULED,
            func.coalesce(model.pickup_tracking_id, "") == "")),
        ("Shipment booking needs review", and_(active, booking(ShippingBooking.state.in_(["not_attempted", "pending", "uncertain"])))),
        ("Shipment action needs confirmation", and_(active, booking(ShippingBooking.action_state.in_(["pending", "submitted", "uncertain", "rejected"])))),
        ("Shipment cancelled or returned — arrange next step", and_(active, booking(func.lower(ShippingBooking.carrier_status).in_(["canceled", "cancelled", "returned"])))),
        ("Tracking update failed", and_(active, booking(ShippingBooking.tracking_error.isnot(None)))),
        ("Email delivery needs attention", exists(select(Notification.id).where(
            Notification.event_key.like(number + ":%"), Notification.sent_at.is_(None),
            or_(Notification.attempts > 0, Notification.created_at < now - timedelta(minutes=15)))).correlate(model)),
    ]
    if kind == "return":
        checks += [
            ("Refund amount needs review", and_(active, or_(model.refund_review_error.isnot(None),
                model.refund_breakdown.is_(None)))),
            ("Gift card needs reconciliation", exists(select(GiftCardIssuance.return_id).where(
                GiftCardIssuance.return_id == model.id, GiftCardIssuance.state.in_(["pending", "uncertain"]))).correlate(model)),
        ]
    else:
        checks.append(("Replacement booking missing", and_(model.status == RequestStatus.COMPLETED,
            model.delivered_at.is_(None), func.coalesce(model.outbound_tracking_id, "") == "")))
        checks.append(("Replacement delivery overdue", and_(model.status == RequestStatus.COMPLETED,
            model.delivered_at.is_(None), model.parcel_decision_at < now - timedelta(days=7))))
    return checks


def predicate(model, kind):
    return or_(*(condition for _, condition in rules(model, kind)))


def reasons_for(obj, kind):
    checks = rules(type(obj), kind)
    values = db.session.execute(select(*(condition for _, condition in checks)).where(type(obj).id == obj.id)).one()
    return [label for (label, _), value in zip(checks, values) if value]
