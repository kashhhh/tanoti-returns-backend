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
CANCELLED = {"cancelled", "canceled", "closed"}
ACTION_PENDING = {"pending", "submitted", "uncertain"}


def current_booking(number, leg):
    return ShippingBooking.query.filter_by(request_number=number, leg=leg).filter(
        ShippingBooking.state != "superseded").order_by(ShippingBooking.attempt.desc()).first()


def is_cancelled(leg, status):
    return (status or "").strip().lower() in (CANCELLED if leg == "pickup" else CANCELLED | {"returned"})


class BookingUncertain(ValueError):
    pass


class BookingRejected(ValueError):
    pass


def booking_failure(result):
    """Translate carrier errors without exposing response bodies or customer data."""
    parts = [result.get(key) for key in ("rmk", "remarks", "message", "error")]
    packages = result.get("packages") or []
    if isinstance(packages, list):
        for package in packages:
            if isinstance(package, dict):
                parts.extend(package.get(key) for key in ("remarks", "rmk", "message"))
    message = " ".join(str(part) for part in parts if part).lower()
    if any(term in message for term in ("insufficient balance", "insufficient fund", "wallet balance", "low balance")):
        return "Wallet recharge needed. Recharge your Delhivery wallet, then retry booking."
    if any(term in message for term in ("not serviceable", "non serviceable", "unserviceable")):
        return "Service unavailable. Check the pincode and that this service is enabled on your Delhivery account."
    if "pickup location" in message or "warehouse" in message:
        return "Warehouse setup needs attention. Check the registered Delhivery pickup location."
    if any(term in message for term in ("pincode", "pin code", "phone", "address")):
        return "Address needs attention. Check the customer address, phone number and pincode."
    return "Booking confirmation pending. Check booking to retrieve the shipment from Delhivery."


class TrackingError(ValueError):
    """A safe, actionable tracking diagnostic, without carrier response bodies."""


def tracking_error_message(exc):
    if isinstance(exc, TrackingError):
        return str(exc)
    if isinstance(exc, (requests.Timeout, TimeoutError)):
        return "Delhivery tracking timed out. Retry shortly; the existing booking is unchanged."
    if isinstance(exc, requests.HTTPError):
        status = exc.response.status_code if exc.response is not None else None
        if status in (401, 403):
            return "Delhivery rejected tracking access. Check the API token and its production/staging environment."
        if status == 429:
            return "Delhivery's tracking rate limit was reached. Wait a few minutes before refreshing."
        if status and status >= 500:
            return "Delhivery's tracking service is temporarily unavailable. Retry shortly."
        return "Delhivery rejected the tracking request. Verify this waybill in Delhivery One."
    if isinstance(exc, requests.ConnectionError):
        return "The server could not connect to Delhivery tracking. Check server connectivity and retry."
    return "Tracking could not be processed. Contact support with this request reference; the existing booking is unchanged."


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


def reference_for(number, leg, attempt=1):
    if leg not in ("pickup", "replacement"):
        raise ValueError("Invalid shipment leg")
    return number + ("-R" if leg == "pickup" else "-F") + (f"-{attempt}" if attempt > 1 else "")


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
    lock_login("shipping:" + reference_for(number, leg))
    booking = current_booking(number, leg)
    reference = booking.reference if booking else reference_for(number, leg)
    payload["shipments"][0]["order"] = reference
    if booking and booking.state != "not_attempted":
        if booking.environment != current_app.config["DELHIVERY_ENVIRONMENT"] or booking.warehouse != current_app.config["DELHIVERY_PICKUP_LOCATION"]:
            raise BookingUncertain("An earlier booking belongs to another Delhivery environment or warehouse. Review it before proceeding.")
        if booking.state == "confirmed":
            if booking.action_state in ACTION_PENDING or is_cancelled(leg, booking.carrier_status):
                raise BookingUncertain("This shipment has a cancellation or pending action. Review it in Delhivery One.")
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
        result = response.json()
        if response.status_code >= 400 and isinstance(result, dict):
            raise BookingUncertain(booking_failure(result))
        response.raise_for_status()
        if not isinstance(result, dict):
            raise BookingUncertain("Booking confirmation pending. Check booking again shortly.")
        packages = result.get("packages") or []
        if result.get("success") is not True or len(packages) != 1:
            raise BookingUncertain(booking_failure(result))
        package = packages[0]
        waybill = str(package.get("waybill") or "")
        if (not re.fullmatch(r"[0-9]{8,32}", waybill) or package.get("status") != "Success"
                or str(package.get("refnum")) != reference):
            raise BookingUncertain(booking_failure(result))
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
        message = str(exc) if isinstance(exc, BookingUncertain) else "Booking confirmation pending. Check booking again shortly; a duplicate will not be created."
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
        existing = current_booking(number, leg)
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
        raise TrackingError("Enter a valid Delhivery waybill")
    try:
        base, headers = _base_url(), _headers()
    except ValueError as exc:
        raise TrackingError(str(exc)) from exc
    response = requests.get(base + "/api/v1/packages/json/", headers=headers,
                            params={"waybill": str(waybill)}, timeout=15)
    response.raise_for_status()
    try:
        result = response.json()
    except ValueError as exc:
        raise TrackingError("Delhivery returned an unreadable tracking response. Retry shortly.") from exc
    if not isinstance(result, dict) or not isinstance(result.get("ShipmentData"), list):
        raise TrackingError("Delhivery returned an unexpected tracking response. Verify the waybill in Delhivery One.")
    shipments = [entry["Shipment"] for entry in result["ShipmentData"]
                 if isinstance(entry, dict) and isinstance(entry.get("Shipment"), dict)]
    matches = [s for s in shipments if str(s.get("AWB")) == str(waybill)]
    if len(matches) != 1:
        raise TrackingError("Delhivery did not return this waybill. Check the waybill and booking environment in Delhivery One.")
    return matches[0]


def reconcile(number, leg, waybill):
    lock_login("shipping:" + reference_for(number, leg))
    booking = current_booking(number, leg)
    if not booking:
        raise ValueError("No API booking attempt exists for this shipment")
    if booking.environment != current_app.config["DELHIVERY_ENVIRONMENT"]:
        raise ValueError("Use the Delhivery environment of the original booking")
    reference = booking.reference
    shipment = fetch_tracking(waybill)
    if str(shipment.get("ReferenceNo")) != reference:
        raise ValueError("This waybill belongs to a different request or shipment leg")
    if booking.waybill and booking.waybill != str(waybill):
        raise ValueError("A different waybill is already confirmed")
    booking.waybill, booking.state, booking.error = str(waybill), "confirmed", None
    record_tracking(booking, shipment)
    booking.updated_at = datetime.utcnow()
    db.session.commit()
    return "delhivery", str(waybill), booking.carrier_status or "scheduled"


def check_booking(number, leg):
    """Recover by exact order reference; an empty lookup is NOT proof of rejection."""
    lock_login("shipping:" + reference_for(number, leg))
    booking = current_booking(number, leg)
    if not booking or booking.environment != current_app.config["DELHIVERY_ENVIRONMENT"]:
        raise BookingUncertain("Use the original booking's Delhivery environment to check its status.")
    if booking.waybill:
        return reconcile(number, leg, booking.waybill)
    try:
        response = requests.get(_base_url() + "/api/v1/packages/json/", headers=_headers(),
                                params={"ref_ids": booking.reference}, timeout=15)
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict) or not isinstance(result.get("ShipmentData"), list):
            raise TrackingError("Delhivery returned an unreadable booking lookup. Try again shortly.")
        shipments = [entry.get("Shipment") for entry in result["ShipmentData"] if isinstance(entry, dict)]
        matches = [shipment for shipment in shipments if isinstance(shipment, dict)
                   and str(shipment.get("ReferenceNo")) == booking.reference]
        if len(matches) != 1 or not re.fullmatch(r"[0-9]{8,32}", str(matches[0].get("AWB") or "")):
            raise BookingUncertain("Delhivery has not returned a unique shipment yet. Try again shortly. If this persists, use Advanced recovery after checking with Delhivery.")
        # Verify the waybill independently before attaching it to this request.
        return reconcile(number, leg, str(matches[0]["AWB"]))
    except BookingUncertain:
        raise
    except Exception as exc:
        raise BookingUncertain(tracking_error_message(exc)) from exc


def confirm_not_created(number, leg, reference, confirmed):
    """Audited operator fallback after Delhivery confirms a missing shipment."""
    from datetime import timedelta
    from app.utils.audit import record_admin_action
    lock_login("shipping:" + reference_for(number, leg))
    booking = current_booking(number, leg)
    if (confirmed is not True or not booking or booking.reference != reference
            or booking.state not in ("pending", "uncertain") or booking.waybill):
        raise BookingUncertain("Refresh this request and confirm with Delhivery that no shipment exists.")
    if booking.environment != current_app.config["DELHIVERY_ENVIRONMENT"]:
        raise BookingUncertain("Use the original booking's Delhivery environment.")
    if datetime.utcnow() - booking.updated_at < timedelta(minutes=2):
        raise BookingUncertain("The booking may still be processing. Wait two minutes and check booking first.")
    # Even the manual fallback must first check for a shipment; outages and
    # malformed responses cannot authorize a new attempt.
    response = requests.get(_base_url() + "/api/v1/packages/json/", headers=_headers(),
                            params={"ref_ids": reference}, timeout=15)
    response.raise_for_status()
    result = response.json()
    if not isinstance(result, dict) or result.get("ShipmentData") != []:
        raise BookingUncertain("The lookup did not confirm an empty result. Use Check booking or contact Delhivery.")
    booking.state = "superseded"
    attempt = booking.attempt + 1
    db.session.add(ShippingBooking(reference=reference_for(number, leg, attempt), request_number=number,
        leg=leg, attempt=attempt, environment=booking.environment, warehouse=booking.warehouse,
        state="not_attempted"))
    record_admin_action("shipment_confirmed_not_created", reference)
    db.session.commit()


def _carrier_date(value):
    from datetime import timedelta, timezone
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        parsed = parsed.replace(tzinfo=timezone(timedelta(hours=5, minutes=30))) if not parsed.tzinfo else parsed
        return parsed.astimezone(timezone.utc).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


def record_tracking(booking, shipment):
    status = shipment.get("Status") or {}
    label = str(status.get("Status") or "").strip()
    booking.carrier_status = label[:64]
    booking.carrier_status_code = str(shipment.get("NSLCode") or "")[:32]
    booking.tracking_checked_at = datetime.utcnow()
    booking.tracking_error = None
    # Store only a sanitized, actionable reason, never the raw tracking payload.
    reason = booking_failure({"message": status.get("Instructions")})
    booking.error = reason if not reason.startswith("Booking confirmation pending") else None
    if booking.action_state not in ("pending", "submitted", "uncertain"):
        return
    scan_date = _carrier_date(status.get("StatusDateTime"))
    # An old carrier scan must not confirm a new action.
    if not scan_date or not booking.action_requested_at or scan_date < booking.action_requested_at.replace(microsecond=0):
        return
    confirmed = booking.action == "cancel" and is_cancelled(booking.leg, label)
    if confirmed:
        booking.action_state, booking.action_error = "confirmed", None
        booking.action_history = (booking.action_history or []) + [{
            "action": booking.action, "state": "confirmed", "at": datetime.utcnow().isoformat() + "Z"}]


def _verified_shipment(req, kind, leg):
    from app.models import RequestStatus
    number = req.return_number if kind == "return" else req.exchange_number
    if leg not in ("pickup", "replacement") or (leg == "replacement" and kind != "exchange"):
        raise ValueError("Choose a pickup or replacement shipment")
    if ((leg == "pickup" and req.status != RequestStatus.PICKUP_SCHEDULED)
            or (leg == "replacement" and (req.status != RequestStatus.COMPLETED or req.delivered_at))):
        raise ValueError("This shipment can no longer be changed from this request")
    lock_login("shipping:" + reference_for(number, leg))
    booking = current_booking(number, leg)
    if not booking or booking.state != "confirmed" or not booking.waybill:
        raise ValueError("Verify the existing Delhivery booking before changing it")
    if booking.environment != current_app.config["DELHIVERY_ENVIRONMENT"]:
        raise ValueError("Use the original booking's Delhivery environment")
    prefix = "pickup" if leg == "pickup" else "outbound"
    if getattr(req, prefix + "_carrier") != "delhivery" or getattr(req, prefix + "_tracking_id") != booking.waybill:
        raise ValueError("The request and current booking do not match. Restore or verify the booking first.")
    shipment = fetch_tracking(booking.waybill)
    if str(shipment.get("ReferenceNo")) != booking.reference:
        raise ValueError("Tracking reference does not match this request")
    record_tracking(booking, shipment)
    prefix = "pickup" if leg == "pickup" else "outbound"
    setattr(req, prefix + "_status", booking.carrier_status[:32])
    return booking, shipment


def cancel_shipment(req, kind, leg):
    from flask import g
    from app.utils.audit import record_admin_action
    booking, shipment = _verified_shipment(req, kind, leg)
    label = (booking.carrier_status or "").lower()
    if is_cancelled(leg, label):
        db.session.commit()
        return
    if booking.action_state in ("pending", "submitted", "uncertain"):
        db.session.commit()
        raise BookingUncertain("A carrier action is already awaiting confirmation. Refresh tracking; do not submit it again.")
    movement = {"picked up", "collected", "in transit", "delivered", "dto", "out for delivery"}
    status_type = (shipment.get("Status") or {}).get("StatusType", "").upper()
    scans = [entry.get("ScanDetail", {}) for entry in shipment.get("Scans", [])]
    moved = any(str(s.get("Scan", "")).lower() in movement or str(s.get("ScanType", "")).upper() in {"PU", "DL", "RT"}
                or (str(s.get("Scan", "")).lower() == "dispatched" and s.get("ScanType") != "PP") for s in scans)
    allowed = label in {"manifested", "scheduled", "open", "pending"} or (label == "dispatched" and status_type == "PP")
    if not allowed or moved or status_type in {"PU", "DL", "RT"}:
        raise ValueError("The parcel may already be moving. Arrange cancellation with Delhivery One or your account contact.")
    booking.action, booking.action_state = "cancel", "pending"
    booking.action_requested_at, booking.action_error = datetime.utcnow(), None
    booking.action_history = (booking.action_history or []) + [{"action": "cancel", "state": "requested",
        "by": g.admin_email, "at": booking.action_requested_at.isoformat() + "Z"}]
    record_admin_action("shipment_cancel_requested", booking.reference)
    db.session.commit()  # Durable action fence before contacting the carrier.
    try:
        response = requests.post(_base_url() + "/api/p/edit", headers=_headers(),
            json={"waybill": booking.waybill, "cancellation": "true"}, timeout=20)
        if response.status_code in (401, 403):
            raise BookingRejected("Delhivery rejected the credentials. Correct them before retrying.")
        response.raise_for_status()
        result = response.json()
        if isinstance(result, dict) and (result.get("status") is False or result.get("success") is False):
            reason = booking_failure(result)
            if reason.startswith("Wallet recharge needed"):
                raise BookingRejected("Cancellation failed: insufficient wallet balance. Recharge your Delhivery wallet, then retry cancellation.")
            raise BookingRejected("Cancellation was rejected. Check status; the existing shipment is still active.")
        # HTTP acceptance is not cancellation. Only authenticated tracking
        # confirming the target status can complete the action.
        booking = db.session.execute(db.select(ShippingBooking).filter_by(reference=booking.reference).with_for_update()).scalar_one()
        if booking.action_state == "pending":
            booking.action_state = "submitted"
        db.session.commit()
        fresh = fetch_tracking(booking.waybill)
        if str(fresh.get("ReferenceNo")) != booking.reference:
            raise ValueError("Tracking mismatch")
        record_tracking(booking, fresh)
        setattr(req, ("pickup" if leg == "pickup" else "outbound") + "_status", booking.carrier_status[:32])
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        message = str(exc) if isinstance(exc, BookingRejected) else "Cancellation is not confirmed. Refresh tracking or check Delhivery One before taking another action."
        ShippingBooking.query.filter_by(reference=booking.reference).filter(
            ShippingBooking.action_state.in_(["pending", "submitted"])).update({
                "action_state": "rejected" if isinstance(exc, BookingRejected) else "uncertain", "action_error": message})
        db.session.commit()
        raise BookingUncertain(message) from exc


def prepare_rebooking(req, kind, leg, expected_reference, stock_ready=False):
    """Fence a new attempt only after authenticated terminal tracking of the old one."""
    from app.utils.audit import record_admin_action
    number = req.return_number if kind == "return" else req.exchange_number
    if not expected_reference:
        raise ValueError("Refresh the request and choose the shipment to rebook")
    booking, shipment = _verified_shipment(req, kind, leg)
    if booking.reference != expected_reference:
        raise BookingUncertain("The booking has changed. Refresh this request before taking another action.")
    if not is_cancelled(leg, booking.carrier_status) or booking.action_state in ACTION_PENDING:
        db.session.commit()
        raise BookingUncertain("Cancellation is not confirmed. Refresh tracking before rebooking.")
    if leg == "replacement" and booking.carrier_status.lower() == "returned" and stock_ready is not True:
        raise ValueError("Confirm that the parcel is back or replacement stock is ready before rebooking.")
    if booking.warehouse != current_app.config.get("DELHIVERY_PICKUP_LOCATION"):
        raise ValueError("Use the original booking's warehouse before rebooking")
    # Validate locally before retiring a recoverable shipment.
    _payload(req.order_item.order, req.order_item, number, leg,
             (req.pickup_address or req.order_item.order.shipping_address) if leg == "pickup" else req.order_item.order.shipping_address,
             req.requested_size if leg == "replacement" else None)
    attempt = booking.attempt + 1
    from app.services.notification_service import queue_update
    queue_update(req, kind, "pickup_cancelled" if leg == "pickup" else "replacement_cancelled")
    booking.state = "superseded"
    fresh = ShippingBooking(reference=reference_for(number, leg, attempt), request_number=number,
        leg=leg, attempt=attempt, environment=booking.environment, warehouse=booking.warehouse, state="not_attempted")
    db.session.add(fresh)
    prefix = "pickup" if leg == "pickup" else "outbound"
    setattr(req, prefix + "_tracking_id", None)
    setattr(req, prefix + "_status", "booking_pending")
    record_admin_action("shipment_rebooking_requested", fresh.reference)
    db.session.commit()  # A crash now leaves a visible, retryable new attempt.


def booking_details(number):
    rows = ShippingBooking.query.filter_by(request_number=number).order_by(ShippingBooking.attempt).all()
    def details(b):
        return {"reference": b.reference, "attempt": b.attempt, "state": b.state, "error": b.error,
                    "environment": b.environment, "waybill": b.waybill,
                    "carrier_status": b.carrier_status, "tracking_error": b.tracking_error,
                    "tracking_checked_at": b.tracking_checked_at.isoformat() + "Z" if b.tracking_checked_at else None,
                    "action": b.action, "action_state": b.action_state, "action_error": b.action_error,
                    "action_history": b.action_history or [],
                    "can_rebook": b.state == "confirmed" and is_cancelled(b.leg, b.carrier_status) and b.action_state not in ACTION_PENDING}
    return {b.leg: {**details(b), "previous_bookings": [details(old) for old in rows if old.leg == b.leg and old.attempt < b.attempt]}
            for b in rows if b.state != "superseded"}


def refresh_tracking(req, kind):
    """Read authenticated tracking. Physical return inspection stays with the admin."""
    from app.models import RequestStatus
    from app.services.notification_service import queue_update
    from app.services.replacement_service import ensure_replacement
    number = req.return_number if kind == "return" else req.exchange_number
    errors = []
    for leg, prefix in (("pickup", "pickup"), ("replacement", "outbound")):
        if leg == "replacement" and kind != "exchange":
            continue
        if leg == "pickup" and req.parcel_received_at:
            continue
        waybill = getattr(req, prefix + "_tracking_id")
        if not waybill or getattr(req, prefix + "_carrier") != "delhivery":
            continue
        booking = current_booking(number, leg)
        if not booking:
            continue  # Manually recorded shipments have no API identity to verify.
        try:
            with db.session.begin_nested():
                if booking.state != "confirmed":
                    raise TrackingError("This booking is not verified. Use Verify Delhivery waybill before refreshing tracking.")
                if booking.environment != current_app.config.get("DELHIVERY_ENVIRONMENT"):
                    raise TrackingError("This booking belongs to a different Delhivery environment. Restore its original environment before tracking.")
                if booking.waybill != waybill:
                    raise TrackingError("Tracking waybill does not match the current booking. Verify the saved booking before continuing.")
                shipment = fetch_tracking(waybill)
                if str(shipment.get("ReferenceNo")) != booking.reference:
                    raise TrackingError("Delhivery returned a different order reference for this waybill. Verify the saved booking in Delhivery One.")
                status = shipment.get("Status") or {}
                label = str(status.get("Status") or "").strip()
                if not label:
                    raise TrackingError("Delhivery returned no shipment status. Retry tracking shortly.")
                if label:
                    setattr(req, prefix + "_status", label[:32])
                record_tracking(booking, shipment)
                if is_cancelled(leg, label):
                    queue_update(req, kind, "pickup_cancelled" if leg == "pickup" else "replacement_cancelled")
                if (leg == "replacement" and label.lower() == "delivered" and status.get("StatusType") == "DL"
                        and req.status == RequestStatus.COMPLETED and not req.delivered_at):
                    # Carrier timestamps without an offset are India local time.
                    date = _carrier_date(status.get("StatusDateTime"))
                    if date is None:
                        raise TrackingError("Delhivery reported delivery without a valid date. Verify delivery before marking it received.")
                    from datetime import timedelta, timezone
                    if date < req.created_at or date > datetime.utcnow() + timedelta(minutes=5):
                        raise TrackingError("Delhivery reported an invalid delivery date. Verify delivery before marking it received.")
                    req.delivered_at = req.completed_at = date
                    req.delivered_by = "Delhivery tracking"
                    ensure_replacement(req)
                    queue_update(req, kind, "delivered")
        except Exception as exc:
            message = tracking_error_message(exc)
            booking.tracking_error = message
            errors.append(f"{leg}: {message}")
            current_app.logger.warning("Delhivery tracking failed for %s (%s): %s", booking.reference, type(exc).__name__, message)
    return errors


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
                ShippingBooking.query.filter_by(request_number=number).filter(ShippingBooking.state != "superseded").update({"updated_at": datetime.utcnow()})
                db.session.commit()
            except Exception:
                db.session.rollback()
                ShippingBooking.query.filter_by(request_number=number).filter(ShippingBooking.state != "superseded").update({"updated_at": datetime.utcnow(),
                    "tracking_error": "Could not refresh Delhivery tracking. Please try again."})
                db.session.commit()
                current_app.logger.warning("Delhivery tracking refresh failed for %s request %s", kind, ident)
    deliver_pending()


def tracking_url(carrier, tracking_id):
    if carrier == "delhivery" and tracking_id:
        return "https://www.delhivery.com/track-v2/package/" + quote(str(tracking_id), safe="")
    return None
