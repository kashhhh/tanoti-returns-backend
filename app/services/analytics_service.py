"""Submission-cohort analytics. Aggregate in SQL; never imply store-wide rates."""
from datetime import datetime, timedelta
from sqlalchemy import select, union_all, func, literal, case
from app.extensions import db
from app.models import ReturnRequest, ExchangeRequest, OrderItem
from app.services.request_listing import branch, STAGES

FINAL = ("refund_paid", "gift_card_issued", "delivered", "rejected")


def overview(args):
    today = datetime.utcnow().date()
    try:
        start = datetime.strptime(args.get("from") or str(today - timedelta(days=29)), "%Y-%m-%d")
        last = datetime.strptime(args.get("to") or str(today), "%Y-%m-%d")
    except ValueError:
        raise ValueError("Dates must use YYYY-MM-DD")
    days = (last - start).days + 1
    if days < 1 or days > 366:
        raise ValueError("Choose a date range between 1 and 366 days")
    kind = args.get("type", "all")
    if kind not in ("all", "return", "exchange"):
        raise ValueError("Invalid request type")
    end = last + timedelta(days=1)
    branches = []
    for model, label in ((ReturnRequest, "return"), (ExchangeRequest, "exchange")):
        branches.append(branch(model, label).add_columns(
            OrderItem.product_title.label("product"),
            (model.requested_size if label == "exchange" else literal("")).label("requested_size"),
            (model.net_refund_amount if label == "return" else literal(0)).label("amount"),
            func.coalesce(model.parcel_decision_at, model.parcel_received_at, model.photo_decision_at, model.created_at).label("stage_since")))
    records = union_all(*branches).subquery()
    filters = [records.c.created_at >= start, records.c.created_at < end]
    if kind != "all":
        filters.append(records.c.kind == kind)
    cohort = select(records).where(*filters).subquery()
    c = cohort.c
    counts = dict.fromkeys(STAGES, 0)
    counts.update(dict(db.session.execute(select(c.stage, func.count()).group_by(c.stage)).all()))
    total = sum(counts.values())
    previous_filters = [records.c.created_at >= start - timedelta(days=days), records.c.created_at < start]
    if kind != "all":
        previous_filters.append(records.c.kind == kind)
    previous = db.session.scalar(select(func.count()).select_from(records).where(*previous_filters))
    def groups(columns, limit=12, where=()):
        result = db.session.execute(select(*columns, func.count().label("count")).where(*where).group_by(*columns).order_by(func.count().desc(), *columns).limit(limit))
        return [dict(row._mapping) for row in result]
    products = groups([c.product_key, c.product, c.size, c.reason, c.kind])
    reasons = groups([c.reason, c.kind], 30)
    sizing = groups([c.product_key, c.product, c.size, c.requested_size], where=(c.kind == "exchange",))
    def amount(stages):
        value = db.session.scalar(select(func.coalesce(func.sum(c.amount), 0)).where(c.stage.in_(stages)))
        return format(value, ".2f")
    daily = dict(db.session.execute(select(func.date(c.created_at), func.count()).group_by(func.date(c.created_at))).all())
    trend = [{"date": str((start + timedelta(days=i)).date()), "count": daily.get(str((start + timedelta(days=i)).date()), 0)} for i in range(days)]
    # PostgreSQL DATE returns date objects; SQLite returns strings.
    daily = {str(key): value for key, value in daily.items()}
    for point in trend:
        point["count"] = daily.get(point["date"], 0)
    now = datetime.utcnow()
    oldest = db.session.execute(select(c.number, c.stage, c.stage_since).where(c.stage.notin_(FINAL)).order_by(c.stage_since, c.number).limit(8)).all()
    waits = [{"number": r.number, "stage": r.stage, "days": max(0, (now-r.stage_since).days)} for r in oldest]
    return {
        "from": str(start.date()), "to": str(last.date()), "type": kind,
        "previous_from": str((start-timedelta(days=days)).date()), "previous_to": str((start-timedelta(days=1)).date()),
        "total": total, "previous_total": previous,
        "active": sum(value for stage, value in counts.items() if stage not in FINAL),
        "successful": sum(counts[s] for s in ("refund_paid", "gift_card_issued", "delivered")),
        "rejected": counts["rejected"], "stages": counts,
        "finance": {"approved_unpaid": amount(("refund_approved",)), "paid": amount(("refund_paid",)), "gift_cards": amount(("gift_card_issued",))},
        "products": products, "reasons": reasons, "sizing": sizing, "trend": trend, "oldest": waits,
    }
