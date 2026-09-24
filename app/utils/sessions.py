"""Opaque, revocable sessions. Only keyed digests of credentials reach the DB."""
import hashlib
import hmac
import re
import secrets
from datetime import datetime, timedelta
from functools import wraps

from flask import current_app, g, has_request_context, jsonify, request
from app.extensions import db
from app.models import AuthSession, Customer


def cookie_name(role):
    prefix = "__Host-" if current_app.config["SESSION_COOKIE_SECURE"] else ""
    return f"{prefix}tanoti_{role}_session"


def digest(value):
    return hmac.new(current_app.config["SECRET_KEY"].encode(), value.encode(), hashlib.sha256).hexdigest()


def csrf_token(raw):
    return digest("csrf:" + raw)


def create_session(role, subject, email):
    raw = secrets.token_urlsafe(32)
    hours = current_app.config["ADMIN_SESSION_HOURS" if role == "admin" else "CUSTOMER_SESSION_HOURS"]
    row = AuthSession(token_hash=digest(raw), role=role, subject=str(subject), identity_email=email,
                      expires_at=datetime.utcnow() + timedelta(hours=hours))
    previous = request.cookies.get(cookie_name(role)) if has_request_context() else None
    if previous:
        AuthSession.query.filter_by(token_hash=digest(previous), role=role).update({"revoked_at": datetime.utcnow()})
    db.session.add(row)
    return raw


def set_session_cookie(response, role, raw):
    response.set_cookie(cookie_name(role), raw, secure=current_app.config["SESSION_COOKIE_SECURE"],
                        httponly=True, samesite="Strict", path="/")
    return response


def clear_session_cookie(response, role):
    response.delete_cookie(cookie_name(role), secure=current_app.config["SESSION_COOKIE_SECURE"],
                           httponly=True, samesite="Strict", path="/")
    return response


def session_required(role):
    def decorate(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            raw = request.cookies.get(cookie_name(role), "")
            if request.headers.get("Authorization") or not re.fullmatch(r"[A-Za-z0-9_-]{43}", raw):
                return jsonify({"error": "Please log in again."}), 401
            row = db.session.get(AuthSession, digest(raw))
            if not row or row.role != role or row.revoked_at or row.expires_at <= datetime.utcnow():
                return jsonify({"error": "Session expired or revoked. Please log in again."}), 401
            if role == "admin":
                if row.subject not in current_app.config["ADMIN_EMAILS"]:
                    return jsonify({"error": "Admin access is no longer allowed."}), 401
                g.admin_email = row.subject
            else:
                customer = db.session.get(Customer, int(row.subject)) if row.subject.isdecimal() else None
                if not customer or customer.email != row.identity_email:
                    return jsonify({"error": "Customer identity changed. Please log in again."}), 401
                g.customer = customer
            if request.method not in {"GET", "HEAD", "OPTIONS"}:
                supplied = request.headers.get("X-CSRF-Token", "")
                if not hmac.compare_digest(supplied.encode(), csrf_token(raw).encode()):
                    return jsonify({"error": "Session verification failed. Refresh the page and try again."}), 403
            g.auth_session = row
            g.csrf_token = csrf_token(raw)
            return fn(*args, **kwargs)
        return wrapper
    return decorate


def logout_response(role, all_sessions=False):
    row = g.auth_session
    if all_sessions:
        AuthSession.query.filter_by(role=role, subject=row.subject).update({"revoked_at": datetime.utcnow()})
    else:
        row.revoked_at = datetime.utcnow()
    db.session.commit()
    return clear_session_cookie(jsonify({"message": "Logged out"}), role)
