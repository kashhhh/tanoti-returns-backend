from flask import Blueprint, request, jsonify, current_app

from app.services import shopify_client, sync_service

webhooks_bp = Blueprint("webhooks", __name__)


def _verify_or_reject():
    if current_app.config["MOCK_SHOPIFY_MODE"]:
        return None  # skip HMAC check in local/mock mode
    hmac_header = request.headers.get("X-Shopify-Hmac-Sha256")
    if not shopify_client.verify_webhook_hmac(request.get_data(), hmac_header):
        return jsonify({"error": "Invalid webhook signature"}), 401
    return None


@webhooks_bp.route("/orders-create", methods=["POST"])
def orders_create():
    rejected = _verify_or_reject()
    if rejected:
        return rejected
    sync_service.upsert_order(request.get_json())
    return jsonify({"ok": True})


@webhooks_bp.route("/orders-updated", methods=["POST"])
def orders_updated():
    rejected = _verify_or_reject()
    if rejected:
        return rejected
    sync_service.upsert_order(request.get_json())
    return jsonify({"ok": True})


@webhooks_bp.route("/fulfillments-create", methods=["POST"])
def fulfillments_create():
    """Shopify's fulfillment webhook payload is the fulfillment object,
    not the full order -- it includes order_id, so we re-fetch/upsert
    via the order payload embedded in most fulfillment webhooks, or you
    can pull the order by ID if not included. Simplest robust approach:
    treat this the same as orders_updated and let upsert_order() pick up
    the fulfillment date from the order's `fulfillments` array."""
    rejected = _verify_or_reject()
    if rejected:
        return rejected
    payload = request.get_json() or {}
    order_id = payload.get("order_id")
    if order_id:
        # Fulfillment webhooks don't include full order/line item data,
        # so pull the order fresh and let upsert_order() set fulfilled_at.
        import requests
        resp = requests.get(
            f"https://{current_app.config['SHOPIFY_STORE_DOMAIN']}/admin/api/"
            f"{current_app.config['SHOPIFY_API_VERSION']}/orders/{order_id}.json",
            headers=shopify_client._headers(),
            timeout=10,
        )
        if resp.ok:
            sync_service.upsert_order(resp.json().get("order", {}))
    return jsonify({"ok": True})
