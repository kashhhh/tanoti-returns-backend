from flask import Flask, send_from_directory, abort
from flask_cors import CORS

from app.config import Config
from app.extensions import db, migrate


def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)

    db.init_app(app)
    migrate.init_app(app, db)
    from app.security import install_security
    install_security(app)
    CORS(
        app,
        supports_credentials=True,
        origins=[origin.strip() for origin in app.config.get("CORS_ORIGINS", "http://localhost:5173").split(",")],
        allow_headers=["Content-Type", "X-CSRF-Token"],
    )

    from app.auth.routes import auth_bp
    from app.customer.routes import customer_bp
    from app.admin.routes import admin_bp
    from app.webhooks.routes import webhooks_bp

    from app.auth.admin_routes import admin_auth_bp
    app.register_blueprint(admin_auth_bp, url_prefix="/api/admin/auth")

    app.register_blueprint(auth_bp, url_prefix="/api/auth")
    app.register_blueprint(customer_bp, url_prefix="/api/customer")
    app.register_blueprint(admin_bp, url_prefix="/api/admin")
    app.register_blueprint(webhooks_bp, url_prefix="/api/webhooks/shopify")

    # Evidence is private. Never expose this directory through an nginx alias.
    from app.utils.admin_auth import admin_required
    @app.route("/uploads/<path:filename>")
    @admin_required
    def uploaded_file(filename):
        import re
        from app.models import ReturnRequest, ExchangeRequest
        match = re.fullmatch(r"returns/((RET|EXC)-[A-Z0-9]+)/[a-f0-9]{32}\.jpg", filename)
        if not match:
            abort(404)
        number, kind = match.groups()
        model, column = (ReturnRequest, ReturnRequest.return_number) if kind == "RET" else (ExchangeRequest, ExchangeRequest.exchange_number)
        record = model.query.filter(column == number).first()
        if not record or "/uploads/" + filename not in (record.photo_urls or []):
            abort(404)
        return send_from_directory(app.config["PHOTOS_STORAGE_DIR"], filename)

    @app.cli.command("sync-shopify-returns")
    def sync_shopify_returns():
        from app.services.shopify_returns import retry_pending
        retry_pending()

    @app.cli.command("backfill-replacements")
    def backfill_replacements():
        from app.models import ExchangeRequest
        from app.services.replacement_service import ensure_replacement
        for req in ExchangeRequest.query.filter(ExchangeRequest.delivered_at.isnot(None)).all():
            ensure_replacement(req)
        db.session.commit()

    @app.cli.command("retry-emails")
    def retry_emails():
        from app.services.notification_service import deliver_pending
        deliver_pending()

    @app.cli.command("sync-delhivery")
    def sync_delhivery():
        from app.services.shipping_service import sync_tracking
        sync_tracking()

    @app.cli.command("cleanup-photos")
    def cleanup_photos():
        from app.services.storage_service import cleanup_old_photos
        cleanup_old_photos()

    return app
