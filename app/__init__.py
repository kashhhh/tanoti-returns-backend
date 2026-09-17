from flask import Flask, send_from_directory
from flask_cors import CORS

from app.config import Config
from app.extensions import db, migrate


def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)

    db.init_app(app)
    migrate.init_app(app, db)
    CORS(
        app,
        supports_credentials=True,
        origins=app.config.get("CORS_ORIGINS", "http://localhost:5173").split(","),
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

    # Serves the photos saved by storage_service.py. In production, it's
    # more efficient to have nginx serve this path directly as static
    # files instead of proxying through Flask -- this route is a
    # perfectly fine default either way.
    @app.route("/uploads/<path:filename>")
    def uploaded_file(filename):
        return send_from_directory(app.config["PHOTOS_STORAGE_DIR"], filename)

    @app.cli.command("retry-emails")
    def retry_emails():
        from app.services.notification_service import deliver_pending
        deliver_pending()

    @app.cli.command("cleanup-photos")
    def cleanup_photos():
        from app.services.storage_service import cleanup_old_photos
        cleanup_old_photos()

    return app
