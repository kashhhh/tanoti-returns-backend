import hashlib
import hmac
import secrets
from datetime import datetime, timedelta

import jwt
from flask import Blueprint, current_app, request, jsonify
from app.extensions import db
from app.models import AdminOTP
from app.services.email_service import send_admin_otp_email

admin_auth_bp = Blueprint("admin_auth", __name__)


def credentials():
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return None
    email = data.get("email", "")
    secret = data.get("secret", "")
    if not isinstance(email, str) or not isinstance(secret, str):
        return None
    email = email.strip().lower()
    expected = current_app.config.get("ADMIN_SECRET_KEY", "")
    if not current_app.config.get("SECRET_KEY") or current_app.config["SECRET_KEY"] == "change-me-in-prod":
        return None
    if not expected or expected == "change-me-in-prod":
        return None
    if not hmac.compare_digest(secret.encode(), expected.encode()) or email not in current_app.config["ADMIN_EMAILS"]:
        return None
    return email


def digest(email, code):
    return hmac.new(current_app.config["SECRET_KEY"].encode(),
                    ("admin:" + email + ":" + code).encode(), hashlib.sha256).hexdigest()


@admin_auth_bp.post("/request-otp")
@admin_auth_bp.post("/resend-otp")
def request_otp():
    email = credentials()
    if not email:
        return jsonify({"error": "Email or admin secret was not accepted"}), 401
    now = datetime.utcnow()
    token = db.session.execute(db.select(AdminOTP).filter_by(email=email).with_for_update()).scalar_one_or_none()
    if token and (now - token.created_at).total_seconds() < current_app.config["OTP_RESEND_COOLDOWN_SECONDS"]:
        return jsonify({"error": "Please wait 30 seconds before requesting another code"}), 429
    if token is None:
        token = AdminOTP(email=email)
        db.session.add(token)
    code = "".join(secrets.choice("0123456789") for _ in range(6))
    token.otp_hash = digest(email, code)
    token.created_at = now
    token.expires_at = now + timedelta(minutes=current_app.config["OTP_EXPIRY_MINUTES"])
    token.attempts = 0
    token.consumed = False
    db.session.commit()
    delivery = "console" if current_app.config.get("TESTING_MODE") else "email"
    try:
        send_admin_otp_email(email, code)
    except Exception:
        current_app.logger.warning("Admin OTP delivery failed")
        return jsonify({"error": "Could not send the code. Please try again shortly."}), 503
    return jsonify({
        "message": "Code available in the backend console (development mode)." if delivery == "console" else "Code sent by email.",
        "delivery": delivery,
        "expires_in_minutes": current_app.config["OTP_EXPIRY_MINUTES"],
    })


@admin_auth_bp.post("/verify-otp")
def verify_otp():
    email = credentials()
    if not email:
        return jsonify({"error": "Email or admin secret was not accepted"}), 401
    code = (request.get_json(silent=True) or {}).get("otp", "")
    if not isinstance(code, str) or len(code.strip()) != 6 or not code.strip().isdigit():
        return jsonify({"error": "Enter the six-digit code"}), 400
    token = db.session.execute(db.select(AdminOTP).filter_by(email=email).with_for_update()).scalar_one_or_none()
    if not token or token.consumed or token.expires_at <= datetime.utcnow():
        return jsonify({"error": "Code expired or already used. Request a new code."}), 400
    if token.attempts >= current_app.config["OTP_MAX_VERIFY_ATTEMPTS"]:
        return jsonify({"error": "Too many attempts. Request a new code."}), 429
    token.attempts += 1
    if not hmac.compare_digest(token.otp_hash, digest(email, code.strip())):
        db.session.commit()
        return jsonify({"error": "Incorrect code"}), 400
    token.consumed = True
    db.session.commit()
    now = datetime.utcnow()
    session = jwt.encode({"sub": email, "role": "admin", "aud": "tanoti-admin", "iat": now,
                          "exp": now + timedelta(hours=current_app.config["ADMIN_SESSION_HOURS"])},
                         current_app.config["SECRET_KEY"], algorithm="HS256")
    return jsonify({"token": session, "email": email})
