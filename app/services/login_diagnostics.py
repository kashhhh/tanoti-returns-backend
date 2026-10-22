"""Read-only login troubleshooting; never emit credentials or contact values."""
from flask import current_app
from sqlalchemy import inspect

from app.extensions import db
from app.models import AdminSettings, Order
from app.services import shopify_client, whatsapp_service


def diagnose(email):
    report = {
        "address_phone_fallback_code": True,
        "whatsapp_configured": whatsapp_service.is_configured(),
        "testing_mode": bool(current_app.config.get("TESTING_MODE")),
        "missing_whatsapp_settings": [key for key in (
            "WHATSAPP_ACCESS_TOKEN", "WHATSAPP_PHONE_NUMBER_ID",
            "WHATSAPP_OTP_TEMPLATE_NAME", "WHATSAPP_OTP_TEMPLATE_LANGUAGE",
        ) if not current_app.config.get(key)],
    }
    try:
        report["scope_migration_applied"] = all(
            "order_scope_id" in {column["name"] for column in inspect(db.engine).get_columns(table)}
            for table in ("otp_tokens", "auth_sessions"))
    except Exception as exc:
        report["database_check_error"] = type(exc).__name__
    try:
        customer = shopify_client.find_customer_by_email(email.strip().lower())
        report["customer_found"] = bool(customer)
        if not customer:
            report["email_login_channels"] = ["email"]
            return report
        account_phone = whatsapp_service.registered_phone(customer)
        report["account_phone_usable"] = bool(account_phone)
        report["default_address_phone_usable"] = bool(whatsapp_service.address_phone(customer, {}))
        report["email_login_channels"] = ["email", "whatsapp"] if (
            account_phone and report["whatsapp_configured"]) else ["email"]
        orders = shopify_client.fetch_orders_for_customer(str(customer["id"]))
    except Exception as exc:
        report["shopify_lookup_error"] = type(exc).__name__
        return report
    report["orders"] = []
    for order in orders:
        linked = order.get("customer") or {}
        email_matches = (order.get("email") or "").strip().lower() == (customer.get("email") or "").strip().lower()
        identity_matches = str(linked.get("id")) == str(customer["id"])
        phone = whatsapp_service.registered_phone(linked) or account_phone
        address_phone = whatsapp_service.address_phone(customer, order)
        row = {
            "order_number": order.get("order_number") or order.get("name"),
            "order_email_matches_account": email_matches,
            "order_customer_matches_account": identity_matches,
            "phone_source": "account" if phone else "address" if address_phone else "none",
            "blockers": [],
        }
        if not report["whatsapp_configured"]:
            row["blockers"].append("whatsapp_not_configured")
        if not email_matches or not identity_matches:
            row["blockers"].append("order_identity_mismatch")
        if not (phone or address_phone):
            row["blockers"].append("no_usable_phone")
        try:
            cached = Order.query.filter_by(shopify_order_id=str(order["id"])).first()
            if cached:
                settings = db.session.get(AdminSettings, 1)
                if settings:
                    reasons = [item.request_block_reason(settings.return_window_days) for item in cached.items]
                    row["cached_eligible_items"] = reasons.count(None)
                    row["cached_item_block_reasons"] = sorted({reason for reason in reasons if reason})
                    if None not in reasons:
                        row["blockers"].append("no_eligible_items_in_local_cache")
            else:
                row["cached_order"] = False
        except Exception as exc:
            row["cached_order_check_error"] = type(exc).__name__
            db.session.rollback()
        report["orders"].append(row)
    return report
