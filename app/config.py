import os
from app.admin_setup import ADMIN_EMAILS
from pathlib import Path

# backend/ directory -- computed from this file's own location, not the
# process's working directory, so storage paths are stable regardless of
# where you launch `python run.py` from (a relative path here previously
# caused uploaded photos to be saved and served from different places).
BASE_DIR = Path(__file__).resolve().parent.parent

class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "")
    APP_ENV = os.environ.get("APP_ENV", "production")
    SESSION_COOKIE_SECURE = APP_ENV != "development"
    CUSTOMER_SESSION_HOURS = 8
    SHOP_CURRENCY = os.environ.get("SHOP_CURRENCY", "INR")
    RATE_LIMIT_ENABLED = True
    TRUST_PROXY = os.environ.get("TRUST_PROXY", "false").lower() == "true"
    ADMIN_API_KEY = os.environ.get("ADMIN_API_KEY", "change-me-in-prod")
    ADMIN_SECRET_KEY = os.environ.get("ADMIN_SECRET_KEY") or os.environ.get("ADMIN_API_KEY", "")
    ADMIN_EMAILS = ADMIN_EMAILS
    ADMIN_SESSION_HOURS = 8
    TESTING_MODE = os.environ.get("TESTING_MODE", "false").lower() == "true"
    MAX_CONTENT_LENGTH = 22 * 1024 * 1024
    MAX_FORM_MEMORY_SIZE = 64 * 1024
    MAX_FORM_PARTS = 20
    CORS_ORIGINS = os.environ.get("CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173")
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL", "postgresql://localhost/tanoti_returns_v2"
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # Shopify
    SHOPIFY_STORE_DOMAIN = os.environ.get("SHOPIFY_STORE_DOMAIN")     # e.g. tanoti-official.myshopify.com
    SHOPIFY_CLIENT_ID = os.environ.get("SHOPIFY_CLIENT_ID")
    SHOPIFY_CLIENT_SECRET = os.environ.get("SHOPIFY_CLIENT_SECRET")
    SHOPIFY_ADMIN_API_TOKEN = os.environ.get("SHOPIFY_ADMIN_API_TOKEN")
    # Native return records/restocking are optional; order imports and cards are independent.
    SHOPIFY_RETURNS_SYNC_ENABLED = False
    SHOPIFY_API_VERSION = os.environ.get("SHOPIFY_API_VERSION", "2026-07")
    SHOPIFY_WEBHOOK_SECRET = os.environ.get("SHOPIFY_WEBHOOK_SECRET")

    # Resend (email)
    RESEND_API_KEY = os.environ.get("RESEND_API_KEY")
    EMAIL_FROM = os.environ.get("EMAIL_FROM", "returns@tanotiofficial.com")

    # Local photo storage (on this app's own disk/VPS -- no third-party
    # storage service). Default lands in backend/uploads; override with an
    # absolute path in production if you want it elsewhere.
    PHOTOS_STORAGE_DIR = os.environ.get("PHOTOS_STORAGE_DIR", str(BASE_DIR / "uploads"))
    # Total budget for that directory, in MB. Once exceeded, the oldest
    # returns' photos are deleted automatically.
    MAX_PHOTO_STORAGE_MB = int(os.environ.get("MAX_PHOTO_STORAGE_MB", "500"))
    PHOTO_RETENTION_DAYS = int(os.environ.get("PHOTO_RETENTION_DAYS", "120"))

    # Delhivery
    DELHIVERY_API_TOKEN = os.environ.get("DELHIVERY_API_TOKEN")
    DELHIVERY_PICKUP_LOCATION = os.environ.get("DELHIVERY_PICKUP_LOCATION")  # your registered warehouse name

    # OTP
    OTP_LENGTH = 6
    OTP_EXPIRY_MINUTES = 10
    OTP_RESEND_COOLDOWN_SECONDS = 30
    OTP_MAX_VERIFY_ATTEMPTS = 5
