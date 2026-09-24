import hashlib
import hmac
import re
import secrets
from datetime import datetime, timedelta

from flask import Blueprint, request, jsonify, current_app

from app.extensions import db
from app.models import Customer, OTPToken
from app.services import shopify_client, email_service, sync_service
from app.utils.decorators import issue_token
from app.security import limited, lock_login

auth_bp = Blueprint("auth", __name__)


def _hash_otp(otp: str) -> str:
    return hmac.new(current_app.config["SECRET_KEY"].encode(),
                    ("customer:" + otp).encode(), hashlib.sha256).hexdigest()


def _email(data):
    email = data.get("email", "") if isinstance(data, dict) else ""
    if not isinstance(email, str):
        return None
    email = email.strip().lower()
    # Excludes Shopify search operators/quotes/whitespace as well as malformed input.
    if len(email) > 254 or not re.fullmatch(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9-]+(?:\.[a-z0-9-]+)+", email):
        return None
    return email


def _sent_response():
    delivery = "console" if current_app.config.get("TESTING_MODE") else "email"
    return jsonify({"message": "If this email is eligible, a code has been sent.",
                    "delivery": delivery, "expires_in_minutes": current_app.config["OTP_EXPIRY_MINUTES"]})


def _generate_otp() -> str:
    length = current_app.config["OTP_LENGTH"]
    return "".join(secrets.choice("0123456789") for _ in range(length))


@auth_bp.route("/request-otp", methods=["POST"])
@auth_bp.route("/resend-otp", methods=["POST"])
def request_otp():
    email = _email(request.get_json(silent=True))
    if not email:
        return jsonify({"error": "Enter a valid email address"}), 400
    blocked = limited("customer-send", email, 5, 900)
    if blocked is not None:
        return blocked
    lock_login("customer:" + email)
    last_token = OTPToken.query.filter_by(email=email).order_by(OTPToken.id.desc()).first()
    cooldown = current_app.config["OTP_RESEND_COOLDOWN_SECONDS"]
    if last_token and (datetime.utcnow() - last_token.created_at).total_seconds() < cooldown:
        return jsonify({"error": "Please wait before requesting another code"}), 429

    # Validate the email actually belongs to a real Tanoti customer before
    # sending anything -- decided requirement.
    try:
        shopify_customer = shopify_client.find_customer_by_email(email)
    except Exception:
        current_app.logger.warning("Customer lookup unavailable")
        return jsonify({"error": "Login is temporarily unavailable. Please try again shortly."}), 503
    if not shopify_customer:
        return _sent_response()

    customer = Customer.query.filter_by(email=email).first()
    if not customer:
        customer = Customer(email=email)
        db.session.add(customer)
    customer.shopify_customer_id = str(shopify_customer["id"])
    customer.name = f"{shopify_customer.get('first_name', '')} {shopify_customer.get('last_name', '')}".strip()
    db.session.flush()
    OTPToken.query.filter_by(email=email, consumed=False).update({"consumed": True})

    otp = _generate_otp()
    token = OTPToken(
        email=email,
        otp_hash=_hash_otp(otp),
        expires_at=datetime.utcnow() + timedelta(minutes=current_app.config["OTP_EXPIRY_MINUTES"]),
    )
    db.session.add(token)
    db.session.commit()

    try:
        email_service.send_otp_email(email, otp)
    except Exception:
        token.consumed = True
        db.session.commit()
        current_app.logger.warning("Customer OTP delivery failed")
        return jsonify({"error": "Could not send the code. Please try again shortly."}), 503
    return _sent_response()


@auth_bp.route("/verify-otp", methods=["POST"])
def verify_otp():
    data = request.get_json(silent=True)
    email = _email(data)
    otp = data.get("otp", "") if isinstance(data, dict) else ""
    if not email or not isinstance(otp, str) or not re.fullmatch(r"[0-9]{6}", otp.strip()):
        return jsonify({"error": "Enter a valid email and six-digit code"}), 400
    otp = otp.strip()
    lock_login("customer:" + email)

    token = (
        OTPToken.query.filter_by(email=email)
        .order_by(OTPToken.id.desc())
        .with_for_update()
        .first()
    )
    if not token or token.consumed or datetime.utcnow() >= token.expires_at:
        return jsonify({"error": "Code invalid or expired. Please request a new one."}), 400

    if token.attempts >= current_app.config["OTP_MAX_VERIFY_ATTEMPTS"]:
        return jsonify({"error": "Too many attempts, please request a new OTP"}), 429

    if not hmac.compare_digest(_hash_otp(otp), token.otp_hash):
        token.attempts += 1
        db.session.commit()
        return jsonify({"error": "Incorrect OTP"}), 400

    token.consumed = True
    db.session.commit()

    customer = Customer.query.filter_by(email=email).first()

    # First-time login: pull their order history so it's ready to browse.
    # (Subsequent syncs happen via webhooks + the manual /resync endpoint.)
    if not customer.orders or any(not order.shipping_address for order in customer.orders):
        sync_service.sync_orders_for_customer(customer.shopify_customer_id)

    jwt_token = issue_token(customer)
    return jsonify({
        "token": jwt_token,
        "customer": {"id": customer.id, "email": customer.email, "name": customer.name},
    })
