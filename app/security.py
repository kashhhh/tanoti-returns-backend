"""Startup checks, response policy and database-backed abuse limits."""
import hashlib
import hmac
import time
from urllib.parse import urlsplit

from flask import current_app, jsonify, request
from app.extensions import db
from app.models import SecurityRateLimit


def validate_config(app):
    if app.testing:
        return
    secret = app.config.get("SECRET_KEY", "")
    if len(secret) < 32 or secret == "change-me-in-prod":
        raise RuntimeError("SECRET_KEY must be an independent random secret of at least 32 characters")
    if app.config.get("APP_ENV") not in {"production", "development"}:
        raise RuntimeError("APP_ENV must be production or development")
    if app.config.get("APP_ENV") == "development":
        return
    if app.debug or app.config.get("TESTING_MODE"):
        raise RuntimeError("Production cannot run with DEBUG or TESTING_MODE")
    if not app.config.get("SESSION_COOKIE_SECURE"):
        raise RuntimeError("Production sessions require Secure cookies")
    admin_secret = app.config.get("ADMIN_SECRET_KEY", "")
    if len(admin_secret) < 32 or admin_secret == secret:
        raise RuntimeError("Production requires an independent ADMIN_SECRET_KEY of at least 32 characters")
    if not app.config.get("RESEND_API_KEY") or not app.config.get("SHOPIFY_WEBHOOK_SECRET"):
        raise RuntimeError("Production requires email delivery and webhook signing credentials")
    for origin in app.config.get("CORS_ORIGINS", "").split(","):
        parsed = urlsplit(origin.strip())
        if parsed.scheme != "https" or not parsed.netloc or parsed.path or parsed.query or parsed.fragment or "*" in origin:
            raise RuntimeError("Production CORS_ORIGINS must contain explicit HTTPS origins")
    if not app.config.get("RATE_LIMIT_ENABLED"):
        raise RuntimeError("Production rate limiting must be enabled")


def limited(scope, identity, limit, seconds):
    """Atomic counters shared by all workers. Never store raw emails/IPs.

    Fixed windows allow a bounded burst across a window boundary. Nginx
    additionally enforces a leaky-bucket per-IP limit at the network edge.
    Uses its own transaction so endpoint rollbacks cannot reset the counter.
    """
    if not current_app.config.get("RATE_LIMIT_ENABLED", True):
        return None
    key = hmac.new(current_app.config["SECRET_KEY"].encode(),
                   f"{scope}:{identity}".encode(), hashlib.sha256).hexdigest()
    now = int(time.time())
    window = now // seconds
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert
    insert = pg_insert if db.engine.dialect.name == "postgresql" else sqlite_insert
    stmt = insert(SecurityRateLimit).values(key=key, window=window, count=1, expires_at=(window + 1) * seconds)
    stmt = stmt.on_conflict_do_update(index_elements=["key", "window"],
                                     set_={"count": SecurityRateLimit.count + 1}).returning(SecurityRateLimit.count)
    with db.engine.begin() as conn:
        count = conn.execute(stmt).scalar_one()
    if count > limit:
        response = jsonify({"error": "Too many requests. Please try again later."})
        response.status_code = 429
        response.headers["Retry-After"] = str((window + 1) * seconds - now)
        return response
    return None


def lock_login(email):
    # Serialize first-time issuance as well as resends across PostgreSQL workers.
    # A row lock alone cannot lock an OTP/customer that does not exist yet.
    if db.engine.dialect.name == "postgresql":
        key = int.from_bytes(hashlib.sha256(email.encode()).digest()[:8], "big", signed=True)
        db.session.execute(db.text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


def install_security(app):
    validate_config(app)
    if app.config.get("TRUST_PROXY"):
        from werkzeug.middleware.proxy_fix import ProxyFix
        # Only enable when the sole ingress is our nginx, which overwrites these.
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=0, x_port=0, x_prefix=0)

    @app.before_request
    def protect_auth():
        unsafe = request.method not in {"GET", "HEAD", "OPTIONS"}
        webhook = request.path.startswith("/api/webhooks/")
        if unsafe and request.path.startswith("/api/") and not webhook:
            origins = {value.strip() for value in app.config["CORS_ORIGINS"].split(",")}
            origin = request.headers.get("Origin")
            if (origin and origin not in origins) or request.headers.get("Sec-Fetch-Site") == "cross-site":
                return jsonify({"error": "Request origin is not allowed"}), 403
            if request.is_json:
                request.max_content_length = 4096 if request.path.startswith(("/api/auth/", "/api/admin/auth/")) else 64 * 1024
                if not isinstance(request.get_json(silent=True), dict):
                    return jsonify({"error": "Expected a JSON object"}), 400
        if request.method == "POST" and request.path.endswith(("/request-otp", "/resend-otp", "/verify-otp")) and request.path.startswith(("/api/auth/", "/api/admin/auth/")):
            request.max_content_length = 4096
            if request.content_length and request.content_length > 4096:
                return jsonify({"error": "Login request is too large"}), 413
            blocked = limited("auth-ip", request.remote_addr or "unknown", 30, 60)
            if blocked is not None:
                return blocked
            data = request.get_json(silent=True)
            if not isinstance(data, dict):
                return jsonify({"error": "Expected a JSON object"}), 400
            email = data.get("email")
            if isinstance(email, str):
                blocked = limited("auth-email", email.strip().lower()[:255], 20, 900)
                if blocked is not None:
                    return blocked

    @app.after_request
    def response_policy(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"
        if request.path.startswith(("/api/", "/uploads/")):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.cli.command("cleanup-security")
    def cleanup_security():
        from datetime import datetime
        from app.models import OTPToken, AuthSession
        SecurityRateLimit.query.filter(SecurityRateLimit.expires_at < int(time.time())).delete()
        OTPToken.query.filter(OTPToken.expires_at < datetime.utcnow()).delete()
        AuthSession.query.filter(AuthSession.expires_at < datetime.utcnow()).delete()
        db.session.commit()

    @app.cli.command("revoke-sessions")
    def revoke_sessions():
        """Emergency revocation of every customer and admin session."""
        from datetime import datetime
        from app.models import AuthSession
        AuthSession.query.filter(AuthSession.revoked_at.is_(None)).update({"revoked_at": datetime.utcnow()})
        db.session.commit()
