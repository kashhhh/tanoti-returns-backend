"""At-most-one create attempt per return, with read-only provider reconciliation."""
import re
import secrets
from datetime import datetime
from decimal import Decimal, InvalidOperation

from flask import current_app, g
from app.extensions import db
from app.models import GiftCardIssuance
from app.services import shopify_client
from app.utils.audit import record_admin_action


class IssuanceUncertain(Exception):
    pass


def validate_card(attempt, req, card, *, creating=False):
    ident = str(card.get("id", ""))
    try:
        amount_matches = Decimal(str(card.get("initial_value"))) == attempt.amount
    except InvalidOperation:
        amount_matches = False
    if (not re.fullmatch(r"[1-9][0-9]{0,19}", ident) or not amount_matches
            or card.get("note") != f"Refund for {req.return_number}"
            or card.get("currency") != current_app.config["SHOP_CURRENCY"]
            or card.get("disabled_at")):
        raise ValueError("Shopify gift card does not match this refund")
    if creating:
        code_matches = str(card.get("code") or "").upper() == attempt.code
    else:
        code_matches = str(card.get("last_characters") or "").upper() == attempt.code[-4:]
    if not code_matches:
        raise ValueError("Shopify gift card code does not match this issuance")
    if GiftCardIssuance.query.filter(GiftCardIssuance.provider_id == ident,
                                      GiftCardIssuance.return_id != req.id).first():
        raise ValueError("This gift card is already assigned to another refund")
    return ident


def confirm(attempt, req, card, *, creating=False):
    attempt.provider_id = validate_card(attempt, req, card, creating=creating)
    attempt.state = "confirmed"
    attempt.confirmed_by = g.admin_email
    attempt.confirmed_at = datetime.utcnow()
    req.gift_card_code = attempt.code
    record_admin_action("gift_card_confirmed" if creating else "gift_card_reconciled", req.return_number)


def issue_once(req):
    attempt = db.session.get(GiftCardIssuance, req.id)
    if (attempt and attempt.state != "rejected") or req.gift_card_code:
        raise IssuanceUncertain("This refund already has an issuance attempt. Reconcile it in Shopify.")
    if req.net_refund_amount <= 0:
        raise ValueError("A gift card requires a positive refund amount")
    attempt = attempt or GiftCardIssuance(return_id=req.id, code=secrets.token_hex(10).upper(),
                               amount=req.net_refund_amount, requested_by=g.admin_email)
    attempt.state = "pending"
    db.session.add(attempt)
    record_admin_action("gift_card_attempted", req.return_number)
    # Commit the fence BEFORE contacting Shopify. A crash or timeout must not
    # erase the evidence that a remote financial action might have succeeded.
    db.session.commit()
    try:
        card = shopify_client.issue_gift_card(amount=str(attempt.amount),
                                             note=f"Refund for {req.return_number}", code=attempt.code)
        # Reacquire the row after releasing it for the external call.
        from app.models import ReturnRequest
        db.session.execute(db.select(ReturnRequest).filter_by(id=req.id).with_for_update()).scalar_one()
        db.session.refresh(attempt)
        if attempt.state != "confirmed":
            confirm(attempt, req, card, creating=True)
    except shopify_client.ShopifyUserError as exc:
        db.session.rollback()
        attempt.state = "rejected"
        req.parcel_decision_at = None
        record_admin_action("gift_card_rejected_by_provider", req.return_number)
        db.session.commit()
        raise ValueError(str(exc).replace(attempt.code, "[card code]")) from exc
    except Exception as exc:
        db.session.rollback()
        # Do not overwrite a simultaneous reconciliation's confirmed state.
        GiftCardIssuance.query.filter_by(return_id=req.id, state="pending").update({"state": "uncertain"})
        record_admin_action("gift_card_uncertain", req.return_number)
        db.session.commit()
        raise IssuanceUncertain("Gift card issuance could not be confirmed. Check the existing card in Shopify; no new card will be created on retry.") from exc
