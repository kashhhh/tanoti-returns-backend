"""Filter and paginate returns and exchanges together in SQL."""
from datetime import datetime, timedelta
from math import ceil
from sqlalchemy import select, literal, case, union_all, func, or_, cast, String
from sqlalchemy.orm import joinedload
from app.extensions import db
from app.models import ReturnRequest, ExchangeRequest, Customer, OrderItem, Order, RequestStatus

STAGES = ("pending", "pickup_pending", "awaiting_parcel", "parcel_received", "refund_approved",
          "gift_card_issued", "refund_paid", "replacement_pending", "shipment_booked", "delivered", "rejected")


def branch(model, kind):
    completed = (case((model.refund_paid_at.isnot(None), "refund_paid"), (func.coalesce(model.gift_card_code, "") != "", "gift_card_issued"), else_="refund_approved")
                 if kind == "return" else
                 case((model.delivered_at.isnot(None), "delivered"), (func.coalesce(model.outbound_tracking_id, "") != "", "shipment_booked"), else_="replacement_pending"))
    stage = case(
        (model.status == RequestStatus.PENDING, "pending"),
        (model.status == RequestStatus.PICKUP_SCHEDULED,
         case((func.coalesce(model.pickup_tracking_id, "") != "", "awaiting_parcel"), else_="pickup_pending")),
        (model.status == RequestStatus.PARCEL_RECEIVED, "parcel_received"),
        (model.status == RequestStatus.REJECTED, "rejected"),
        else_=completed)
    number = model.return_number if kind == "return" else model.exchange_number
    return select(model.id.label("id"), literal(kind).label("kind"), model.created_at.label("created_at"),
                  model.customer_id.label("customer_id"), number.label("number"), stage.label("stage"),
                  Customer.email.label("email"), Order.order_number.label("order_number"),
                  func.coalesce(func.nullif(OrderItem.shopify_product_id, ""), OrderItem.product_title).label("product_key"),
                  OrderItem.size.label("size"), cast(model.reason, String).label("reason")).select_from(model).join(
                      Customer, model.customer_id == Customer.id).join(OrderItem, model.order_item_id == OrderItem.id).join(
                          Order, OrderItem.order_id == Order.id)


def list_page(args):
    try:
        page = int(args.get("page", 1))
        per_page = int(args.get("per_page", 25))
    except (TypeError, ValueError):
        raise ValueError("Page and page size must be whole numbers")
    if page < 1 or per_page not in (10, 25, 50):
        raise ValueError("Page must be positive; page size must be 10, 25 or 50")
    stage = args.get("stage", "all")
    kind = args.get("type", "all")
    sort = args.get("sort", "oldest")
    if stage not in (*STAGES, "all") or kind not in ("return", "exchange", "all") or sort not in ("oldest", "newest"):
        raise ValueError("Invalid request filter")
    records = union_all(branch(ReturnRequest, "return"), branch(ExchangeRequest, "exchange")).subquery()
    filters = []
    if kind != "all":
        filters.append(records.c.kind == kind)
    query = args.get("q", "").strip()[:255].lstrip("#")
    if query:
        pattern = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        filters.append(or_(*(records.c[key].ilike(pattern, escape="\\") for key in ("number", "email", "order_number"))))
    for key in ("product_key", "size", "reason"):
        if args.get(key):
            filters.append(records.c[key] == args[key])
    dates = {}
    for key in ("from", "to"):
        if args.get(key):
            try:
                dates[key] = datetime.strptime(args[key], "%Y-%m-%d")
            except ValueError:
                raise ValueError("Dates must use YYYY-MM-DD")
    if dates.get("from") and dates.get("to") and dates["from"] > dates["to"]:
        raise ValueError("Start date must be before end date")
    if "from" in dates:
        filters.append(records.c.created_at >= dates["from"])
    if "to" in dates:
        filters.append(records.c.created_at < dates["to"] + timedelta(days=1))
    counts = dict.fromkeys(STAGES, 0)
    counts.update(dict(db.session.execute(select(records.c.stage, func.count()).where(*filters).group_by(records.c.stage)).all()))
    counts["all"] = sum(counts.values())
    total = counts[stage]
    pages = max(1, ceil(total / per_page))
    page = min(page, pages)
    if stage != "all":
        filters.append(records.c.stage == stage)
    order = records.c.created_at.asc() if sort == "oldest" else records.c.created_at.desc()
    keys = db.session.execute(select(records.c.id, records.c.kind).where(*filters).order_by(
        order, records.c.kind.asc(), records.c.id.asc()).offset((page-1)*per_page).limit(per_page)).all()
    objects = {}
    for model, label in ((ReturnRequest, "return"), (ExchangeRequest, "exchange")):
        ids = [key.id for key in keys if key.kind == label]
        if ids:
            loaded = model.query.options(joinedload(model.customer), joinedload(model.order_item).joinedload(OrderItem.order)).filter(model.id.in_(ids)).all()
            objects.update({(label, obj.id): obj for obj in loaded})
    customer_ids = {obj.customer_id for obj in objects.values()}
    repeats = {}
    if customer_ids:
        cutoff = datetime.utcnow() - timedelta(days=90)
        repeats = dict(db.session.execute(select(records.c.customer_id, func.count()).where(
            records.c.customer_id.in_(customer_ids), records.c.created_at >= cutoff).group_by(records.c.customer_id)).all())
    return [(objects[(key.kind, key.id)], key.kind) for key in keys], {
        "page": page, "per_page": per_page, "pages": pages, "total": total, "counts": counts,
    }, {ident: count >= 3 for ident, count in repeats.items()}
