import hashlib
import random
from datetime import datetime, timedelta

from flask import Blueprint, request, jsonify, current_app

from app.extensions import db
from app.models import Customer, OTPToken
from app.services import shopify_client, email_service, sync_service
from app.utils.decorators import issue_token

auth_bp = Blueprint("auth", __name__)


def _hash_otp(otp: str) -> str:
    return hashlib.sha256(otp.encode()).hexdigest()


def _generate_otp() -> str:
    length = current_app.config["OTP_LENGTH"]
    return "".join(str(random.randint(0, 9)) for _ in range(length))


@auth_bp.route("/request-otp", methods=["POST"])
def request_otp():
    email = (request.json or {}).get("email", "").strip().lower()
    if not email:
        return jsonify({"error": "Email is required"}), 400

    # Validate the email actually belongs to a real Tanoti customer before
    # sending anything -- decided requirement.
    shopify_customer = shopify_client.find_customer_by_email(email)
    if not shopify_customer:
        return jsonify({"error": "No orders found for this email"}), 404

    customer = Customer.query.filter_by(email=email).first()
    if not customer:
        customer = Customer(email=email)
        db.session.add(customer)
    customer.shopify_customer_id = str(shopify_customer["id"])
    customer.name = f"{shopify_customer.get('first_name', '')} {shopify_customer.get('last_name', '')}".strip()
    db.session.commit()

    otp = _generate_otp()
    token = OTPToken(
        email=email,
        otp_hash=_hash_otp(otp),
        expires_at=datetime.utcnow() + timedelta(minutes=current_app.config["OTP_EXPIRY_MINUTES"]),
    )
    db.session.add(token)
    db.session.commit()

    email_service.send_otp_email(email, otp)
    return jsonify({"message": "OTP sent", "expires_in_minutes": current_app.config["OTP_EXPIRY_MINUTES"]})


@auth_bp.route("/resend-otp", methods=["POST"])
def resend_otp():
    email = (request.json or {}).get("email", "").strip().lower()
    if not email:
        return jsonify({"error": "Email is required"}), 400

    last_token = (
        OTPToken.query.filter_by(email=email)
        .order_by(OTPToken.created_at.desc())
        .first()
    )
    cooldown = current_app.config["OTP_RESEND_COOLDOWN_SECONDS"]
    if last_token and (datetime.utcnow() - last_token.created_at).total_seconds() < cooldown:
        wait = cooldown - int((datetime.utcnow() - last_token.created_at).total_seconds())
        return jsonify({"error": f"Please wait {wait}s before requesting another code"}), 429

    return request_otp()


@auth_bp.route("/verify-otp", methods=["POST"])
def verify_otp():
    data = request.json or {}
    email = data.get("email", "").strip().lower()
    otp = data.get("otp", "").strip()
    if not email or not otp:
        return jsonify({"error": "Email and OTP are required"}), 400

    token = (
        OTPToken.query.filter_by(email=email, consumed=False)
        .order_by(OTPToken.created_at.desc())
        .first()
    )
    if not token:
        return jsonify({"error": "No active OTP for this email, please request a new one"}), 400

    if datetime.utcnow() > token.expires_at:
        return jsonify({"error": "OTP expired, please request a new one"}), 400

    if token.attempts >= current_app.config["OTP_MAX_VERIFY_ATTEMPTS"]:
        return jsonify({"error": "Too many attempts, please request a new OTP"}), 429

    if _hash_otp(otp) != token.otp_hash:
        token.attempts += 1
        db.session.commit()
        return jsonify({"error": "Incorrect OTP"}), 400

    token.consumed = True
    db.session.commit()

    customer = Customer.query.filter_by(email=email).first()

    # First-time login: pull their order history so it's ready to browse.
    # (Subsequent syncs happen via webhooks + the manual /resync endpoint.)
    if not customer.orders:
        sync_service.sync_orders_for_customer(customer.shopify_customer_id)

    jwt_token = issue_token(customer)
    return jsonify({
        "token": jwt_token,
        "customer": {"id": customer.id, "email": customer.email, "name": customer.name},
    })
