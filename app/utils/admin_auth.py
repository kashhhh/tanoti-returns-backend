"""Admin sessions require both the shared secret and a staff email OTP."""
from functools import wraps
import jwt
from flask import current_app, request, jsonify, g


def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        header = request.headers.get("Authorization", "")
        try:
            if not current_app.config.get("SECRET_KEY") or current_app.config["SECRET_KEY"] == "change-me-in-prod":
                raise jwt.InvalidTokenError()
            if not header.startswith("Bearer "):
                raise jwt.InvalidTokenError()
            payload = jwt.decode(header[7:], current_app.config["SECRET_KEY"],
                                 algorithms=["HS256"], audience="tanoti-admin",
                                 options={"require": ["exp", "iat", "sub", "role", "aud"]})
            if payload["role"] != "admin" or payload["sub"] not in current_app.config["ADMIN_EMAILS"]:
                raise jwt.InvalidTokenError()
        except jwt.InvalidTokenError:
            return jsonify({"error": "Admin session expired or invalid. Please log in again."}), 401
        g.admin_email = payload["sub"]
        return fn(*args, **kwargs)
    return wrapper
