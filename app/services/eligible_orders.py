"""Admin order listing using the same per-unit return eligibility rules."""
from datetime import datetime, timedelta
from math import ceil

from sqlalchemy import func, or_

from app.extensions import db
from app.models import AdminSettings, Customer, Order, OrderItem


def list_eligible_orders(args):
    try:
        page = int(args.get("page", 1))
        per_page = int(args.get("per_page", 25))
    except (TypeError, ValueError):
        raise ValueError("Page and page size must be whole numbers")
    if page < 1 or per_page not in (10, 25, 50):
        raise ValueError("Page must be positive; page size must be 10, 25 or 50")
    now = datetime.utcnow()
    days = AdminSettings.get().return_window_days
    eligible = db.session.query(
        OrderItem.order_id.label("order_id"), func.count(OrderItem.id).label("eligible_units"),
        func.min(OrderItem.delivered_at).label("delivered_at"),
        func.max(OrderItem.delivered_at).label("latest_delivered_at"),
    ).filter(
        OrderItem.delivered_at >= now - timedelta(days=days),
        OrderItem.request_claimed.is_(False), OrderItem.unavailable.is_(False),
        OrderItem.superseded.is_(False), ~OrderItem.return_requests.any(),
        ~OrderItem.exchange_requests.any(),
    ).group_by(OrderItem.order_id).subquery()
    records = db.session.query(
        Order.id, Order.order_number, Order.shopify_order_id,
        Customer.name.label("customer_name"), Customer.email.label("customer_email"),
        eligible.c.eligible_units, eligible.c.delivered_at, eligible.c.latest_delivered_at,
    ).join(eligible, eligible.c.order_id == Order.id).join(Customer, Order.customer_id == Customer.id)
    query = args.get("q", "").strip()[:255].lstrip("#")
    if query:
        pattern = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        records = records.filter(or_(*(column.ilike(pattern, escape="\\") for column in
            (Order.order_number, Order.shopify_order_id, Customer.name, Customer.email))))
    total = records.count()
    pages = max(1, ceil(total / per_page))
    page = min(page, pages)
    rows = records.order_by(eligible.c.delivered_at.asc(), Order.id.asc()).offset(
        (page - 1) * per_page).limit(per_page).all()
    return {
        "orders": [{"id": row.id, "order_number": row.order_number,
                    "shopify_order_id": row.shopify_order_id, "customer_name": row.customer_name,
                    "customer_email": row.customer_email, "eligible_units": row.eligible_units,
                    "delivered_at": row.delivered_at.isoformat() + "Z",
                    "latest_delivered_at": row.latest_delivered_at.isoformat() + "Z",
                    "return_deadline": (row.delivered_at + timedelta(days=days)).isoformat() + "Z"}
                   for row in rows],
        "page": page, "per_page": per_page, "pages": pages, "total": total,
        "return_window_days": days,
    }
