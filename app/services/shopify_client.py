"""
Thin wrapper around the Shopify Admin API.

As of January 2026, Dev Dashboard custom apps no longer expose a static
Admin API access token -- installing the app gives you a Client ID and
Client Secret instead, and the app must exchange those for a short-lived
(24h) access token via the client credentials grant, then re-fetch it
before it expires. This module handles that automatically; you only need
SHOPIFY_CLIENT_ID and SHOPIFY_CLIENT_SECRET in .env.

(If you happen to have an older, legacy custom app that still shows a
static token, set SHOPIFY_ADMIN_API_TOKEN instead and that takes priority
-- no token exchange needed in that case.)

Required scopes on the app: read_customers, read_orders, write_gift_cards,
read_gift_cards
"""
import base64
import hashlib
import hmac
import time

import requests
from flask import current_app

# In-memory cache for the client-credentials access token. Per-process --
# fine for a single Flask process; if you later run multiple gunicorn
# workers each will fetch/cache its own token independently, which works
# but isn't a shared cache.
_token_cache = {"token": None, "expires_at": 0}


def verify_webhook_hmac(request_data: bytes, hmac_header: str) -> bool:
    """Validates the X-Shopify-Hmac-Sha256 header on incoming webhooks
    using the signing secret shown on the store's Settings > Notifications
    > Webhooks page (independent of the client credentials flow below)."""
    secret = current_app.config.get("SHOPIFY_WEBHOOK_SECRET", "")
    digest = hmac.new(secret.encode(), request_data, hashlib.sha256).digest()
    computed = base64.b64encode(digest).decode()
    return hmac.compare_digest(computed, hmac_header or "")


def _get_access_token() -> str:
    legacy_token = current_app.config.get("SHOPIFY_ADMIN_API_TOKEN")
    if legacy_token:
        return legacy_token

    now = time.time()
    if _token_cache["token"] and now < _token_cache["expires_at"] - 60:
        return _token_cache["token"]

    resp = requests.post(
        f"https://{current_app.config['SHOPIFY_STORE_DOMAIN']}/admin/oauth/access_token",
        data={
            "grant_type": "client_credentials",
            "client_id": current_app.config["SHOPIFY_CLIENT_ID"],
            "client_secret": current_app.config["SHOPIFY_CLIENT_SECRET"],
        },
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    _token_cache["token"] = data["access_token"]
    _token_cache["expires_at"] = now + data.get("expires_in", 86400)
    return _token_cache["token"]


def _base_url():
    return (
        f"https://{current_app.config['SHOPIFY_STORE_DOMAIN']}"
        f"/admin/api/{current_app.config['SHOPIFY_API_VERSION']}"
    )




def _headers():
    return {
        "X-Shopify-Access-Token": _get_access_token(),
        "Content-Type": "application/json",
    }


def find_customer_by_email(email: str):
    """Used at login time to validate the email actually belongs to a
    real Tanoti customer before sending an OTP."""
    if current_app.config["MOCK_SHOPIFY_MODE"]:
        # Local testing: any email "logs in" as a fake Shopify customer.
        # Pair with scripts/seed.py, which creates a matching local
        # Customer + Order for a specific test email.
        return {"id": "mock-1000", "email": email, "first_name": "Test", "last_name": "Customer"}

    resp = requests.get(
        f"{_base_url()}/customers/search.json",
        headers=_headers(),
        params={"query": f"email:{email}"},
        timeout=10,
    )
    resp.raise_for_status()
    customers = resp.json().get("customers", [])
    return customers[0] if customers else None


def fetch_orders_for_customer(shopify_customer_id: str):
    """Pulls orders for a customer -- used by the sync job / on-demand
    refresh so the local Order/OrderItem cache stays current."""
    if current_app.config["MOCK_SHOPIFY_MODE"]:
        # In mock mode, test orders come from scripts/seed.py directly --
        # nothing to pull from a real store.
        return []

    resp = requests.get(
        f"{_base_url()}/customers/{shopify_customer_id}/orders.json",
        headers=_headers(),
        params={"status": "any"},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json().get("orders", [])

def get_product_image_url(shopify_product_id: str):
    """Order line items don't include image data in Shopify's REST API --
    the image has to be fetched separately from the product. Used by
    sync_service to fill in OrderItem.image_url."""
    if current_app.config["MOCK_SHOPIFY_MODE"] or not shopify_product_id:
        return None

    resp = requests.get(
        f"{_base_url()}/products/{shopify_product_id}.json",
        headers=_headers(),
        params={"fields": "image"},
        timeout=10,
    )
    if not resp.ok:
        return None  # don't fail the whole sync over one missing/deleted product
    image = (resp.json().get("product") or {}).get("image") or {}
    return image.get("src")

def get_variants_for_product(shopify_product_id: str):
    """Live stock check used by get_order_item() to decide which sizes
    are selectable in the exchange dropdown -- out-of-stock sizes get
    excluded per the 'block the option in the dropdown' decision."""
    if current_app.config["MOCK_SHOPIFY_MODE"]:
        return [
            {"size": "S", "in_stock": True},
            {"size": "M", "in_stock": True},
            {"size": "L", "in_stock": False},
            {"size": "XL", "in_stock": True},
        ]

    resp = requests.get(
        f"{_base_url()}/products/{shopify_product_id}/variants.json",
        headers=_headers(),
        timeout=10,
    )
    resp.raise_for_status()
    variants = resp.json().get("variants", [])
    return [
        {"size": v.get("option1"), "in_stock": (v.get("inventory_quantity") or 0) > 0}
        for v in variants
    ]


def issue_gift_card(amount: str, note: str = ""):
    """Creates a Shopify gift card for the given amount (as a decimal
    string, e.g. '1499.00') and returns its code + admin GID.
    Called when the owner accepts a return with refund_mode = gift_card.
    """
    if current_app.config["MOCK_SHOPIFY_MODE"]:
        return {"code": f"MOCK-GC-{amount.replace('.', '')}"}

    payload = {
        "gift_card": {
            "initial_value": amount,
            "note": note,
        }
    }
    resp = requests.post(
        f"{_base_url()}/gift_cards.json",
        headers=_headers(),
        json=payload,
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["gift_card"]
