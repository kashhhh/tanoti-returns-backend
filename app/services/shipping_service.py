"""
Shipping integration, currently implemented against Delhivery. Used at
two points in the two-gate approval flow:
  - schedule_reverse_pickup(): fires once the owner accepts based on
    photos, to bring the item back for inspection.
  - schedule_forward_shipment(): fires once the owner accepts an
    EXCHANGE's returned parcel after inspection, to send the new
    size/item out to the customer.

Both return (carrier, tracking_id, status) and raise on failure -- callers
catch the exception and fall back to "manual_scheduling_required" rather
than blocking the owner's accept action, since a shipping API hiccup
shouldn't stop the workflow.

To add a second carrier (e.g. Shipway) later: add a SHIPPING_CARRIER
config value, branch on it in both functions below, and keep the same
(carrier, tracking_id, status) return shape so nothing else has to change.
"""
import requests
from flask import current_app


def _headers():
    return {
        "Authorization": f"Token {current_app.config['DELHIVERY_API_TOKEN']}",
        "Content-Type": "application/json",
    }


def schedule_reverse_pickup(*, order, item, request_number: str, pickup_address=None):
    """order/item: local Order/OrderItem model instances.
    Returns (carrier, tracking_id, status) or raises on failure."""
    address = pickup_address or order.shipping_address or {}
    payload = {
        "pickup_location": current_app.config["DELHIVERY_PICKUP_LOCATION"],
        "name": address.get("name") or order.customer.name,
        "add": ", ".join(filter(None, [address.get("address1"), address.get("address2")])),
        "phone": address.get("phone", ""),
        "city": address.get("city", ""),
        "state": address.get("province", ""),
        "pin": address.get("zip", ""),
        "country": address.get("country", ""),
        "order": request_number,
        "products": [{"name": item.product_title, "sku": item.sku, "quantity": 1}],
    }
    resp = requests.post(
        "https://track.delhivery.com/api/cmu/create.json",
        headers=_headers(),
        json=payload,
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("pickup_id"):
        raise ValueError("Pickup booking was not confirmed by the carrier")
    return "delhivery", data["pickup_id"], data.get("status", "scheduled")


def schedule_forward_shipment(*, order, item, requested_size: str, request_number: str):
    """Ships the replacement item to the customer once an exchange's
    returned parcel has been inspected and accepted. order/item: local
    Order/OrderItem model instances for the ORIGINAL item -- the new
    size/variant to ship is requested_size.

    NOTE: this uses Delhivery's standard forward-shipment (manifest)
    endpoint as a starting point -- confirm the exact payload shape
    against your Delhivery account's docs, and confirm the new variant's
    SKU/stock lookup (this currently reuses the original item's SKU
    pattern, which likely needs a real Shopify variant lookup by size to
    get the correct SKU for the replacement)."""
    address = order.shipping_address or {}
    payload = {
        "shipments": [{
            "name": order.customer.name,
            "add": ", ".join(filter(None, [address.get("address1"), address.get("address2")])),
            "phone": address.get("phone", ""),
            "city": address.get("city", ""),
            "state": address.get("province", ""),
            "pin": address.get("zip", ""),
            "country": address.get("country", ""),
            "order": request_number,
            "payment_mode": "Prepaid",  # exchanges don't collect payment again
            "products_desc": f"{item.product_title} (exchange, size {requested_size})",
            "quantity": 1,
        }]
    }
    resp = requests.post(
        "https://track.delhivery.com/api/cmu/create.json",
        headers=_headers(),
        json=payload,
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    packages = data.get("packages", [{}])
    waybill = packages[0].get("waybill") if packages else None
    if not waybill:
        raise ValueError("Replacement booking was not confirmed by the carrier")
    return "delhivery", waybill, data.get("status", "scheduled")


def tracking_url(carrier: str, tracking_id: str):
    """Best-effort public tracking link for the customer-facing UI."""
    if not tracking_id:
        return None
    if carrier == "delhivery":
        return f"https://www.delhivery.com/track-v2/package/{tracking_id}"
    return None