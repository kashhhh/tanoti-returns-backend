"""Authenticated, transactionally deduplicated Shopify webhook ingestion."""
import hashlib
import re
from flask import Blueprint, request, jsonify, current_app
from app.extensions import db
from app.models import WebhookReceipt
from app.security import lock_login
from app.services import shopify_client, sync_service

webhooks_bp = Blueprint("webhooks", __name__)


def _receive(topic):
    request.max_content_length = 2 * 1024 * 1024
    raw = request.get_data()
    if not shopify_client.verify_webhook_hmac(raw, request.headers.get("X-Shopify-Hmac-Sha256")):
        return jsonify({"error": "Invalid webhook signature"}), 401
    shop = current_app.config.get("SHOPIFY_STORE_DOMAIN", "")
    if request.headers.get("X-Shopify-Shop-Domain", "").lower() != (shop or "").lower() or not shop:
        return jsonify({"error": "Unexpected Shopify store"}), 401
    delivery = request.headers.get("X-Shopify-Webhook-Id", "")
    if request.headers.get("X-Shopify-Topic") != topic or not re.fullmatch(r"[A-Za-z0-9-]{1,128}", delivery):
        return jsonify({"error": "Invalid webhook metadata"}), 400
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "Expected an order or fulfillment object"}), 400
    ident = payload.get("order_id" if topic == "fulfillments/create" else "id")
    if not re.fullmatch(r"[1-9][0-9]{0,19}", str(ident)):
        return jsonify({"error": "Invalid order ID"}), 400
    # HMAC signs only the body. A captured body with a changed delivery ID must
    # still be deduplicated; unsigned headers are not replay-proof identifiers.
    fingerprint = hashlib.sha256(topic.encode() + b":" + raw).hexdigest()
    lock_login("webhook:" + fingerprint)
    if db.session.get(WebhookReceipt, fingerprint):
        return jsonify({"ok": True, "duplicate": True})
    try:
        if topic == "fulfillments/create":
            payload = shopify_client.fetch_order(str(ident))
            if str(payload.get("id")) != str(ident):
                raise ValueError("Fetched order identity did not match")
        if not isinstance(payload.get("updated_at"), str):
            raise ValueError("Order timestamp is required")
        sync_service._utc(payload["updated_at"])
        sync_service.upsert_order(payload, commit=False)
        db.session.add(WebhookReceipt(fingerprint=fingerprint, delivery_id=delivery, topic=topic))
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.warning("Shopify webhook processing failed (%s)", topic)
        # Never acknowledge a failed import: Shopify must be able to retry it.
        return jsonify({"error": "Webhook processing failed; retry delivery"}), 503
    return jsonify({"ok": True})


@webhooks_bp.post("/orders-create")
def orders_create():
    return _receive("orders/create")


@webhooks_bp.post("/orders-updated")
def orders_updated():
    return _receive("orders/updated")


@webhooks_bp.post("/fulfillments-create")
def fulfillments_create():
    return _receive("fulfillments/create")
