"""Queue emails in the status transaction, then send after commit."""
from datetime import datetime
from html import escape
import requests
from flask import current_app
from app.extensions import db
from app.models import Notification
from app.services.shipping_service import tracking_url


def queue_update(obj, kind, event):
    obj.shopify_sync_status = "pending"
    number = obj.return_number if kind == "return" else obj.exchange_number
    key = f"{number}:{event}"
    if Notification.query.filter_by(event_key=key).first():
        return
    text = {
        "refund_paid": ("Refund paid", "Your refund has been marked paid by our team."),
        "delivered": ("Replacement delivered", "Your replacement has been marked delivered. Your exchange is now complete."),
        "received": ("Request received", "We have received your request and will review your photos."),
        "pickup_pending": ("Request approved", "Your photos have been approved. We are arranging your pickup."),
        "pickup_booked": ("Pickup scheduled", "Your photos have been approved and your reverse pickup is scheduled."),
        "parcel_received": ("Parcel received", "Your parcel has arrived and is awaiting inspection."),
        "rejected": ("Request not accepted", "Your request could not be accepted."),
        "gift_card": ("Gift card issued", "Your parcel was accepted and your gift card has been issued."),
        "refund_approved": ("Refund approved", "Your parcel was accepted. Your refund will be handled manually and may take up to 7 business days."),
        "replacement_booked": ("Replacement shipment booked", "Your parcel was accepted and a replacement shipment has been booked."),
        "replacement_pending": ("Exchange approved", "Your parcel was accepted. We are arranging your replacement shipment."),
    }
    subject, message = text[event]
    html = f"<h2>{subject}</h2><p>{escape(message)}</p><p>Request: <strong>{escape(number)}</strong><br>Item: {escape(obj.order_item.product_title)}<br>Order: #{escape(obj.order_item.order.order_number)}</p>"
    if event == "rejected":
        stage = "photo review" if obj.rejected_stage == "photo" else "parcel inspection"
        html += f"<p>Rejected at {stage}. Reason: {escape(obj.rejection_reason.value.replace('_', ' '))}</p>"
        if obj.rejection_note:
            html += f"<p>{escape(obj.rejection_note)}</p>"
    if event in ("gift_card", "refund_approved", "refund_paid"):
        html += f"<p>Refund amount: INR {escape(str(obj.net_refund_amount))}</p>"
    if event == "gift_card":
        html += f"<p>Gift card code: <strong>{escape(obj.gift_card_code)}</strong></p>"
    if event in ("pickup_booked", "replacement_booked"):
        outbound = event == "replacement_booked"
        carrier = obj.outbound_carrier if outbound else obj.pickup_carrier
        tracking = obj.outbound_tracking_id if outbound else obj.pickup_tracking_id
        html += f"<p>Tracking number: {escape(tracking or '')}</p>"
        url = tracking_url(carrier, tracking)
        if url:
            html += f'<p><a href="{escape(url, quote=True)}">Track shipment</a></p>'
    db.session.add(Notification(event_key=key, recipient=obj.customer.email,
                                subject=f"{subject} - {number}", html=html))


def deliver_pending(limit=50):
    # Each row is locked until its attempt is recorded; concurrent workers skip it.
    ids = [row.id for row in Notification.query.filter_by(sent_at=None).order_by(Notification.id).limit(limit).all()]
    for ident in ids:
        note = db.session.execute(db.select(Notification).filter_by(id=ident, sent_at=None)
                                  .with_for_update(skip_locked=True)).scalar_one_or_none()
        if note is None:
            continue
        note.attempts += 1
        try:
            if current_app.config.get("TESTING_MODE"):
                current_app.logger.info("[LOCAL EMAIL] %s", note.subject)
            else:
                if not current_app.config.get("RESEND_API_KEY"):
                    raise RuntimeError("Email is not configured")
                response = requests.post("https://api.resend.com/emails", headers={
                    "Authorization": f"Bearer {current_app.config['RESEND_API_KEY']}",
                    "Idempotency-Key": f"tanoti-returns/{note.event_key}",
                }, json={"from": current_app.config["EMAIL_FROM"], "to": [note.recipient],
                         "subject": note.subject, "html": note.html}, timeout=10)
                response.raise_for_status()
            note.sent_at = datetime.utcnow()
        except Exception:
            current_app.logger.warning("Email attempt failed for notification %s; queued for retry", note.id)
        db.session.commit()
