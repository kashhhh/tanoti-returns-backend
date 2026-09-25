"""Delhivery B2C shipments with durable protection against duplicate creation.

Reverse shipments schedule customer collection automatically. Replacements use
Tanoti's existing regular warehouse collection; no extra pickup requests are made.
"""
import json
import re
from datetime import datetime
from decimal import Decimal
from urllib.parse import quote

import requests
from flask import current_app
from app.extensions import db
from app.models import ShippingBooking
from app.security import lock_login

HOSTS = {"production": "https://track.delhivery.com", "staging": "https://staging-express.delhivery.com"}
PARCEL = {"shipment_length": 30, "shipment_width": 20, "shipment_height": 4, "weight": 200}


class BookingUncertain(ValueError):
    pass


class BookingRejected(ValueError):
    pass


def _base_url():
    environment = current_app.config.get("DELHIVERY_ENVIRONMENT")
    if environment not in HOSTS:
        raise ValueError("Set DELHIVERY_ENVIRONMENT to staging or production.")
    if current_app.config.get("APP_ENV") == "production" and environment != "production" and not current_app.testing:
        raise ValueError("Set DELHIVERY_ENVIRONMENT=production for the live returns app.")
    return HOSTS[environment]


def _headers():
    token = current_app.config.get("DELHIVERY_API_TOKEN")
    if not token:
        raise ValueError("Configure DELHIVERY_API_TOKEN on the backend.")
    return {"Authorization": f"Token {token}", "Accept": "application/json"}


def reference_for(number, leg):
    if leg not in ("pickup", "replacement"):
        raise ValueError("Invalid shipment leg")
    return number + ("-R" if leg == "pickup" else "-F")


def _payload(order, item, number, leg, address, requested_size=None):
    _base_url()
    _headers()
    warehouse = current_app.config.get("DELHIVERY_PICKUP_LOCATION")
    if not warehouse or not warehouse.strip():
        raise ValueError("Configure the exact registered DELHIVERY_PICKUP_LOCATION name.")
    if (address.get("country") or "").strip().lower() not in ("india", "in"):
        raise ValueError("Delhivery booking currently supports Indian addresses only.")
    phone = re.sub(r"\D", "", address.get("phone") or "")
    if len(phone) == 12 and phone.startswith("91"):
        phone = phone[2:]
    if not re.fullmatch(r"[0-9]{10}", phone) or not re.fullmatch(r"[1-9][0-9]{5}", address.get("zip") or ""):
        raise ValueError("A valid Indian phone number and six-digit pincode are required.")
    if any(not address.get(field) for field in ("address1", "city", "province")):
        raise ValueError("Complete the customer's shipping address before booking.")
    if Decimal(str(item.price)) >= 50000:
        raise ValueError("Arrange this high-value shipment in Delhivery One with its required e-waybill.")
    description = item.product_title
    if requested_size:
        description += f" (replacement, size {requested_size})"
    shipment = {
        "name": address.get("name") or order.customer.name,
        "add": ", ".join(filter(None, [address.get("address1"), address.get("address2")])),
        "phone": phone, "city": address["city"], "state": address["province"],
        "pin": address["zip"], "country": "India", "order": reference_for(number, leg),
        "payment_mode": "Pickup" if leg == "pickup" else "Prepaid",
        "products_desc": description, "quantity": 1,
        "total_amount": str(item.price), "cod_amount": "0", **PARCEL,
    }
    # Do not mislabel the replacement with the original size's SKU.
    for config, field in (("DELHIVERY_SELLER_GST", "seller_gst_tin"), ("DELHIVERY_HSN_CODE", "hsn_code")):
        if current_app.config.get(config):
            shipment[field] = current_app.config[config]
    # No return_* overrides: reverse parcels go to the registered warehouse.
    return {"pickup_location": {"name": warehouse}, "shipments": [shipment]}


def _manifest(payload, number, leg):
    reference = reference_for(number, leg)
    lock_login("shipping:" + reference)
    booking = db.session.get(ShippingBooking, reference)
    if booking and booking.state != "not_attempted":
        if booking.environment != current_app.config["DELHIVERY_ENVIRONMENT"] or booking.warehouse != current_app.config["DELHIVERY_PICKUP_LOCATION"]:
            raise BookingUncertain("An earlier booking belongs to another Delhivery environment or warehouse. Review it before proceeding.")
        if booking.state == "confirmed":
            return booking.waybill
        raise BookingUncertain("Delhivery may already have created this shipment. Verify its existing waybill; do not book another.")
    booking = booking or ShippingBooking(reference=reference, request_number=number, leg=leg)
    booking.environment = current_app.config["DELHIVERY_ENVIRONMENT"]
    booking.warehouse = current_app.config["DELHIVERY_PICKUP_LOCATION"]
    booking.state, booking.error = "pending", None
    db.session.add(booking)
    db.session.commit()  # Persist before HTTP, including when a worker dies mid-call.
    try:
        response = requests.post(_base_url() + "/api/cmu/create.json", headers=_headers(),
            data={"format": "json", "data": json.dumps(payload)}, timeout=20)
        if response.status_code in (401, 403):
            raise BookingRejected("Delhivery rejected the API credentials. Check the token and environment, then retry.")
        response.raise_for_status()
        result = response.json()
        packages = result.get("packages") or []
        if result.get("success") is not True or len(packages) != 1:
            raise BookingUncertain("Delhivery did not confirm the shipment. It may have been partially saved; check Delhivery One.")
        package = packages[0]
        waybill = str(package.get("waybill") or "")
        if (not re.fullmatch(r"[0-9]{8,32}", waybill) or package.get("status") != "Success"
                or str(package.get("refnum")) != reference):
            raise BookingUncertain("Delhivery's response did not match this shipment. Verify the waybill in Delhivery One.")
        booking = db.session.execute(db.select(ShippingBooking).filter_by(reference=reference).with_for_update()).scalar_one()
        if booking.state == "confirmed" and booking.waybill != waybill:
            raise BookingUncertain("A different waybill was already reconciled. Review this shipment in Delhivery One.")
        booking.waybill, booking.state, booking.error = waybill, "confirmed", None
        booking.updated_at = datetime.utcnow()
        db.session.commit()
        return waybill
    except BookingRejected as exc:
        db.session.rollback()
        ShippingBooking.query.filter_by(reference=reference, state="pending").update({"state": "not_attempted", "error": str(exc), "updated_at": datetime.utcnow()})
        db.session.commit()
        raise
    except Exception as exc:
        db.session.rollback()
        message = str(exc) if isinstance(exc, BookingUncertain) else "Delhivery booking could not be confirmed. Check Delhivery One before taking further action."
        ShippingBooking.query.filter_by(reference=reference, state="pending").update({"state": "uncertain", "error": message, "updated_at": datetime.utcnow()})
        db.session.commit()
        raise BookingUncertain(message) from exc


def schedule_reverse_pickup(*, order, item, request_number, pickup_address=None):
    return _schedule(order, item, request_number, "pickup", pickup_address or order.shipping_address or {})


def schedule_forward_shipment(*, order, item, requested_size, request_number):
    return _schedule(order, item, request_number, "replacement", order.shipping_address or {}, requested_size)


def _schedule(order, item, number, leg, address, size=None):
    try:
        payload = _payload(order, item, number, leg, address, size)
    except ValueError as exc:
        reference = reference_for(number, leg)
        lock_login("shipping:" + reference)
        existing = db.session.get(ShippingBooking, reference)
        if not existing or existing.state == "not_attempted":
            existing = existing or ShippingBooking(reference=reference, request_number=number, leg=leg,
                environment=current_app.config.get("DELHIVERY_ENVIRONMENT", "staging"),
                warehouse=current_app.config.get("DELHIVERY_PICKUP_LOCATION") or "", state="not_attempted")
            existing.error = str(exc)[:500]
            db.session.add(existing)
            db.session.commit()
        raise
    return "delhivery", _manifest(payload, number, leg), "scheduled"


def fetch_tracking(waybill):
    if not re.fullmatch(r"[0-9]{8,32}", str(waybill)):
        raise ValueError("Enter a valid Delhivery waybill")
    response = requests.get(_base_url() + "/api/v1/packages/json/", headers=_headers(),
                            params={"waybill": str(waybill)}, timeout=15)
    response.raise_for_status()
    shipments = [entry.get("Shipment", {}) for entry in response.json().get("ShipmentData", [])]
    matches = [s for s in shipments if str(s.get("AWB")) == str(waybill)]
    if len(matches) != 1:
        raise ValueError("Delhivery did not return this waybill")
    return matches[0]


def reconcile(number, leg, waybill):
    reference = reference_for(number, leg)
    booking = db.session.execute(db.select(ShippingBooking).filter_by(reference=reference).with_for_update()).scalar_one_or_none()
    if not booking:
        raise ValueError("No API booking attempt exists for this shipment")
    if booking.environment != current_app.config["DELHIVERY_ENVIRONMENT"]:
        raise ValueError("Use the Delhivery environment of the original booking")
    shipment = fetch_tracking(waybill)
    if str(shipment.get("ReferenceNo")) != reference:
        raise ValueError("This waybill belongs to a different request or shipment leg")
    if (shipment.get("Status") or {}).get("Status", "").lower() in ("cancelled", "canceled"):
        raise ValueError("This shipment has been cancelled")
    if booking.waybill and booking.waybill != str(waybill):
        raise ValueError("A different waybill is already confirmed")
    booking.waybill, booking.state, booking.error = str(waybill), "confirmed", None
    booking.updated_at = datetime.utcnow()
    db.session.commit()
    return "delhivery", str(waybill), "scheduled"


def booking_details(number):
    return {b.leg: {"reference": b.reference, "state": b.state, "error": b.error,
                    "environment": b.environment, "waybill": b.waybill}
            for b in ShippingBooking.query.filter_by(request_number=number)}


def refresh_tracking(req, kind):
    """Read authenticated tracking. Physical return inspection stays with the admin."""
    from app.models import RequestStatus
    from app.services.notification_service import queue_update
    from app.services.replacement_service import ensure_replacement
    number = req.return_number if kind == "return" else req.exchange_number
    for leg, prefix in (("pickup", "pickup"), ("replacement", "outbound")):
        if leg == "replacement" and kind != "exchange":
            continue
        waybill = getattr(req, prefix + "_tracking_id")
        if not waybill or getattr(req, prefix + "_carrier") != "delhivery":
            continue
        booking = db.session.get(ShippingBooking, reference_for(number, leg))
        if not booking or booking.state != "confirmed" or booking.environment != current_app.config["DELHIVERY_ENVIRONMENT"]:
            continue
        shipment = fetch_tracking(waybill)
        if str(shipment.get("ReferenceNo")) != booking.reference:
            raise ValueError("Tracking reference does not match this request")
        status = shipment.get("Status") or {}
        label = str(status.get("Status") or "")
        if label:
            setattr(req, prefix + "_status", label[:32])
        if (leg == "replacement" and label.lower() == "delivered" and status.get("StatusType") == "DL"
                and req.status == RequestStatus.COMPLETED and not req.delivered_at):
            # Carrier timestamps without an offset are India local time.
            date = datetime.fromisoformat(str(status.get("StatusDateTime", "")).replace("Z", "+00:00"))
            from datetime import timedelta, timezone
            date = date.replace(tzinfo=timezone(timedelta(hours=5, minutes=30))) if not date.tzinfo else date
            date = date.astimezone(timezone.utc).replace(tzinfo=None)
            if date < req.created_at or date > datetime.utcnow() + timedelta(minutes=5):
                raise ValueError("Invalid carrier delivery time")
            req.delivered_at = req.completed_at = date
            req.delivered_by = "Delhivery tracking"
            ensure_replacement(req)
            queue_update(req, kind, "delivered")


def sync_tracking(limit=100):
    from app.models import ReturnRequest, ExchangeRequest, RequestStatus
    from app.services.notification_service import deliver_pending
    for model, kind in ((ReturnRequest, "return"), (ExchangeRequest, "exchange")):
        query = model.query.filter(model.status.in_([RequestStatus.PICKUP_SCHEDULED, RequestStatus.PARCEL_RECEIVED, RequestStatus.COMPLETED]))
        if kind == "return":
            query = query.filter(model.parcel_received_at.is_(None), model.pickup_tracking_id.isnot(None))
        else:
            query = query.filter(model.delivered_at.is_(None), db.or_(model.pickup_tracking_id.isnot(None), model.outbound_tracking_id.isnot(None)))
        column = model.return_number if kind == "return" else model.exchange_number
        ids = list(dict.fromkeys(row.id for row in query.join(ShippingBooking,
            ShippingBooking.request_number == column).filter(ShippingBooking.state == "confirmed")
            .order_by(ShippingBooking.updated_at).limit(limit)))
        for ident in ids:
            req = db.session.execute(db.select(model).filter_by(id=ident).with_for_update(skip_locked=True)).scalar_one_or_none()
            if req is None:
                continue
            number = req.return_number if kind == "return" else req.exchange_number
            try:
                refresh_tracking(req, kind)
                ShippingBooking.query.filter_by(request_number=number).update({"updated_at": datetime.utcnow()})
                db.session.commit()
            except Exception:
                db.session.rollback()
                ShippingBooking.query.filter_by(request_number=number).update({"updated_at": datetime.utcnow()})
                db.session.commit()
                current_app.logger.warning("Delhivery tracking refresh failed for %s request %s", kind, ident)
    deliver_pending()


def tracking_url(carrier, tracking_id):
    if carrier == "delhivery" and tracking_id:
        return "https://www.delhivery.com/track-v2/package/" + quote(str(tracking_id), safe="")
    return None
