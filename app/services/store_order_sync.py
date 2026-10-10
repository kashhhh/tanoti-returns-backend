"""Import every accessible recent store order, with a durable pagination cursor."""
from datetime import datetime, timedelta
from flask import current_app

from app.extensions import db
from app.models import StoreOrderSync
from app.security import lock_login
from app.services import shopify_client
from app.services.sync_service import upsert_order


def sync_status():
    state = db.session.get(StoreOrderSync, 1)
    completed = state.completed_at if state else None
    return {"running": bool(state and state.running),
            "processed": state.processed if state else 0,
            "skipped": state.skipped if state else 0,
            "error": state.error if state else None,
            "last_completed_at": completed.isoformat() + "Z" if completed else None,
            "requires_sync": bool(state and (state.running or state.error)) or not completed or
                             completed < datetime.utcnow() - timedelta(minutes=15),
            "lookback_days": 60, "store_domain": current_app.config.get("SHOPIFY_STORE_DOMAIN")}


def sync_store_batch():
    """Commit one page atomically; resume that page if a provider call fails."""
    try:
        lock_login("store-order-import")
        state = db.session.get(StoreOrderSync, 1)
        if state is None:
            state = StoreOrderSync(id=1, running=False, processed=0, skipped=0)
            db.session.add(state)
        if not state.running:
            state.started_at = datetime.utcnow()
            state.processed = state.skipped = 0
            state.next_page = None
            state.running = True
        db.session.flush()
        orders, next_page = shopify_client.fetch_store_orders_page(state.next_page,
            since=(state.started_at - timedelta(days=60)).isoformat() + "Z")
        # One batch delivery lookup serves all orders on the page. Store imports
        # avoid product/image calls; customer browsing still refreshes images.
        ids = [f["id"] for order in orders for f in order.get("fulfillments", [])
               if f.get("id") and f.get("status") not in ("cancelled", "failure", "error")]
        delivery_cache = shopify_client.fetch_fulfillment_delivery_dates(ids) if ids else {}
        for payload in orders:
            imported = upsert_order(payload, commit=False, delivery_cache=delivery_cache, refresh_images=False)
            state.processed += 1
            if imported is None:
                state.skipped += 1
        state.next_page = next_page
        state.error = None
        state.running = bool(next_page)
        if not state.running:
            state.completed_at = datetime.utcnow()
        db.session.commit()
        return sync_status()
    except Exception:
        db.session.rollback()
        lock_login("store-order-import")
        state = db.session.get(StoreOrderSync, 1)
        if state is None:
            state = StoreOrderSync(id=1, running=False, processed=0, skipped=0)
            db.session.add(state)
        state.error = "Shopify import failed. Retry to resume; the last completed import is still shown."
        db.session.commit()
        raise


def sync_store_orders():
    result = sync_store_batch()
    while result["running"]:
        result = sync_store_batch()
    return result
