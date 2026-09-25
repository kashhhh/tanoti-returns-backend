"""Queue emails in the status transaction, then send after commit."""
from datetime import datetime
from html import escape
import requests
from flask import current_app
from app.extensions import db
from app.models import Notification
from app.services.shipping_service import tracking_url
from app.services.email_templates import layout

EMAIL_EVENTS = {"received", "pickup_booked", "replacement_booked", "delivered", "gift_card", "refund_paid"}


def queue_update(obj, kind, event):
    if current_app.config.get("SHOPIFY_RETURNS_SYNC_ENABLED"):
        obj.shopify_sync_status = "pending"
    number = obj.return_number if kind == "return" else obj.exchange_number
    key = f"{number}:{event}"
    if Notification.query.filter_by(event_key=key).first():
        return
    from app.utils.audit import record_admin_action
    record_admin_action(event, number)
    if event not in EMAIL_EVENTS:
        return
    text = {
        "refund_paid": ("Your refund has been issued", "Our team has issued your refund. The time it takes to appear in your account depends on your bank or payment provider."),
        "delivered": ("Your replacement has arrived", "Your replacement has been confirmed delivered and your exchange is complete. We hope it is just right for you."),
        "received": ("We have received your request", "Thank you for sending your request. Our team will review the details and photos. You can check its progress in the returns portal."),
        "pickup_booked": ("Your pickup is arranged", "Your return pickup has been arranged. Please keep the item ready with its original tags and packaging."),
        "gift_card": ("Your Tanoti gift card is ready", "Your return has been accepted. Use the gift card code below at checkout on your next Tanoti order. Keep this code private."),
        "replacement_booked": ("Your replacement is arranged", "Your return has been accepted and shipment of your replacement has been arranged. Follow its progress using the tracking details below."),
    }
    subject, message = text[event]
    html = f"<p>{escape(message)}</p><p style='background:#f5f2ed;padding:16px;border-radius:8px'>Request: <strong>{escape(number)}</strong><br>Item: {escape(obj.order_item.product_title)}<br>Variant: {escape(obj.order_item.size or '—')}<br>Order: #{escape(obj.order_item.order.order_number)}</p>"
    if event in ("gift_card", "refund_paid"):
        html += f"<p>Amount: <strong>{escape(current_app.config['SHOP_CURRENCY'])} {obj.net_refund_amount:.2f}</strong></p>"
    if event == "gift_card":
        html += f"<p>Gift card code</p><p style='background:#f5f2ed;padding:18px;text-align:center;font-size:22px;font-weight:bold;letter-spacing:2px;word-break:break-all'>{escape(obj.gift_card_code)}</p>"
    if event in ("pickup_booked", "replacement_booked"):
        outbound = event == "replacement_booked"
        carrier = obj.outbound_carrier if outbound else obj.pickup_carrier
        tracking = obj.outbound_tracking_id if outbound else obj.pickup_tracking_id
        html += f"<p>Tracking number: {escape(tracking or '')}</p>"
        url = tracking_url(carrier, tracking)
        if url:
            html += f'<p><a href="{escape(url, quote=True)}">Track shipment</a></p>'
    db.session.add(Notification(event_key=key, recipient=obj.customer.email,
                                subject=f"{subject} · {number}", html=layout(subject, html)))


def deliver_pending(limit=50):
    # Each row is locked until its attempt is recorded; concurrent workers skip it.
    # Retire unsent legacy status emails so a deployment cannot send old chatter.
    for note in Notification.query.filter_by(sent_at=None).all():
        if note.event_key.rsplit(":", 1)[-1] not in EMAIL_EVENTS:
            db.session.delete(note)
    db.session.commit()
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
