"""Customer login codes through Meta's approved copy-code authentication template."""
import re

import requests
from flask import current_app


def registered_phone(customer):
    # Only the customer account phone may grant account access. Never use an
    # order/shipping contact or a number supplied by the login request.
    value = customer.get("phone")
    if not isinstance(value, str):
        return None
    value = re.sub(r"[\s().-]", "", value)
    return value if re.fullmatch(r"\+[1-9][0-9]{7,14}", value) else None


def is_configured():
    return bool(current_app.config.get("TESTING_MODE") or all(
        current_app.config.get(key) for key in (
            "WHATSAPP_ACCESS_TOKEN", "WHATSAPP_PHONE_NUMBER_ID",
            "WHATSAPP_OTP_TEMPLATE_NAME", "WHATSAPP_OTP_TEMPLATE_LANGUAGE",
        )
    ))


def send_otp_whatsapp(phone, code):
    if not registered_phone({"phone": phone}):
        raise ValueError("A registered international phone number is required")
    if current_app.config.get("TESTING_MODE"):
        print(f"[LOCAL WHATSAPP] OTP for {phone}: {code}", flush=True)
        return "console"
    if not is_configured():
        raise RuntimeError("Configure WhatsApp and an approved authentication template")
    config = current_app.config
    response = requests.post(
        f"https://graph.facebook.com/{config['WHATSAPP_API_VERSION']}/{config['WHATSAPP_PHONE_NUMBER_ID']}/messages",
        headers={"Authorization": f"Bearer {config['WHATSAPP_ACCESS_TOKEN']}"},
        json={
            "messaging_product": "whatsapp", "recipient_type": "individual",
            "to": phone.lstrip("+"), "type": "template",
            "template": {
                "name": config["WHATSAPP_OTP_TEMPLATE_NAME"],
                "language": {"code": config["WHATSAPP_OTP_TEMPLATE_LANGUAGE"]},
                "components": [
                    {"type": "body", "parameters": [{"type": "text", "text": code}]},
                    {"type": "button", "sub_type": "url", "index": "0",
                     "parameters": [{"type": "text", "text": code}]},
                ],
            },
        }, timeout=10,
    )
    # Do not propagate provider response bodies containing contacts or codes.
    if not response.ok or not response.json().get("messages"):
        raise RuntimeError("WhatsApp did not accept the login message")
    return "whatsapp"
