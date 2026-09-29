"""Read-only Shopify valuation. Never initiates a payment or changes issued credit."""
from datetime import datetime
from decimal import Decimal, InvalidOperation

from flask import current_app
from app.extensions import db
from app.models import ExchangeRequest, GiftCardIssuance, RefundMode
from app.services import shopify_client


def money(value):
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0 or amount > Decimal("99999999.99") or amount != amount.quantize(Decimal("0.01")):
            raise ValueError()
        return amount
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError("Shopify returned an invalid money amount. Review the order before refunding.")


def share(amount, count, unit):
    """Allocate indivisible paise deterministically, preserving the line total."""
    paise, remainder = divmod(int(amount * 100), count)
    return Decimal(paise + (unit <= remainder)) / 100


def source_item(item):
    seen = set()
    while item.replacement_for:
        if item.id in seen:
            raise ValueError("Replacement history needs review before refunding.")
        seen.add(item.id)
        exchange = ExchangeRequest.query.filter_by(exchange_number=item.replacement_for).first()
        if not exchange or not exchange.delivered_at:
            raise ValueError("Replacement delivery must be confirmed before refunding.")
        item = exchange.order_item
    return item


def unit_value(order, line_id, unit, currency):
    if not isinstance(order, dict) or not isinstance(order.get("refunds"), list) or not isinstance(order.get("line_items"), list):
        raise ValueError("Shopify order details are incomplete. Please refresh before refunding.")
    if order.get("currency") != currency:
        raise ValueError("Order currency does not match the store. Review this refund manually.")
    if order.get("cancelled_at") or order.get("financial_status") not in ("paid", "partially_refunded"):
        raise ValueError("Shopify does not show this order as paid. Confirm payment in Shopify first.")
    # Partial refunds do not identify our individual unit cards. Do not guess
    # which physical unit was refunded, or allocate unassigned cash refunds.
    for refund in order.get("refunds", []):
        lines = refund.get("refund_line_items") or []
        if any(str(row.get("line_item_id")) == str(line_id) for row in lines):
            raise ValueError("This item has a refund recorded in Shopify. Review it before issuing more credit.")
        if not lines:
            raise ValueError("This order has an unallocated Shopify refund. Review it before issuing more credit.")
    matches = [line for line in order.get("line_items", []) if str(line.get("id")) == str(line_id)]
    if len(matches) != 1:
        raise ValueError("The original purchased item could not be verified in Shopify.")
    line = matches[0]
    count = line.get("quantity")
    if not isinstance(count, int) or isinstance(count, bool) or not 1 <= unit <= count:
        raise ValueError("Shopify item quantity changed. Review this refund.")
    if "current_quantity" in line and line["current_quantity"] != count:
        raise ValueError("Shopify item quantity was adjusted. Review this refund before issuing credit.")
    if (not isinstance(order.get("taxes_included"), bool) or not isinstance(line.get("tax_lines"), list)
            or not isinstance(line.get("discount_allocations"), list)):
        raise ValueError("Shopify pricing details are incomplete. Refresh the order before refunding.")
    price = money(line.get("price"))
    discount = sum((money(d.get("amount")) for d in line["discount_allocations"]), Decimal(0))
    tax = sum((money(t.get("price")) for t in line["tax_lines"]), Decimal(0))
    if discount > price * count:
        raise ValueError("Shopify discounts exceed the item value. Review the order.")
    paid_total = price * count - discount + (Decimal(0) if order["taxes_included"] else tax)
    paid = share(paid_total, count, unit)
    # Derive the allocated discount from the allocated paid/tax values so
    # displayed components add up exactly, including odd-paise quantities.
    unit_tax = share(tax, count, unit)
    allocated_discount = price + (Decimal(0) if order["taxes_included"] else unit_tax) - paid
    return {"currency": currency, "item_price": str(price), "discount": str(allocated_discount),
            "tax": str(unit_tax), "tax_included": order["taxes_included"], "paid_amount": str(paid),
            "source_line_id": str(line_id), "unit_number": unit, "shipping_refund": "0.00"}


def quote_item(item):
    original = source_item(item)
    try:
        order = shopify_client.fetch_order(original.order.shopify_order_id)
    except Exception as exc:
        raise ValueError("Could not verify the paid amount with Shopify. Please try again later.") from exc
    if not isinstance(order, dict) or str(order.get("id")) != original.order.shopify_order_id:
        raise ValueError("Shopify returned a different order. Refund verification stopped.")
    quote = unit_value(order, original.shopify_line_item_id, original.unit_number, current_app.config["SHOP_CURRENCY"])
    quote["checked_at"] = datetime.utcnow().isoformat() + "Z"
    return quote


def verify_request(req):
    quote = quote_item(req.order_item)
    if money(quote["paid_amount"]) != req.refund_amount:
        raise ValueError("The paid item amount differs from this refund. Use Review refund amount before approving.")
    expected = max(Decimal(0), req.refund_amount - req.deduction_applied)
    if expected != req.net_refund_amount:
        raise ValueError("The refund deduction needs review. Use Review refund amount before approving.")
    req.refund_breakdown = quote
    req.refund_review_error = None
    return quote


def can_revalue(req):
    if req.completed_at and req.net_refund_amount == 0:
        raise ValueError("This request is already complete with no refund due.")
    attempt = db.session.get(GiftCardIssuance, req.id)
    legacy_uncertain = req.refund_mode == RefundMode.GIFT_CARD and req.parcel_decision_at and not attempt
    if req.refund_paid_at or req.gift_card_code or legacy_uncertain or (attempt and attempt.state != "rejected"):
        raise ValueError("An issued or uncertain refund cannot be changed. Reconcile the existing payment first.")


def apply_quote(req, quote):
    can_revalue(req)
    req.refund_amount = money(quote["paid_amount"])
    req.deduction_applied = min(req.deduction_applied, req.refund_amount)
    req.net_refund_amount = req.refund_amount - req.deduction_applied
    req.refund_breakdown = quote
    req.refund_review_error = None
    attempt = db.session.get(GiftCardIssuance, req.id)
    if attempt:  # Definitively rejected attempts may safely use the corrected amount.
        attempt.amount = req.net_refund_amount
