"""Revocable sessions, webhook receipts and durable gift-card attempts."""
from alembic import op
import sqlalchemy as sa

revision = "c94a8e2fd061"
down_revision = "78ad903bc612"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("orders", sa.Column("shopify_updated_at", sa.DateTime()))
    op.create_table("admin_audit",
                    sa.Column("id", sa.Integer(), primary_key=True),
                    sa.Column("actor", sa.String(255), nullable=False),
                    sa.Column("action", sa.String(64), nullable=False),
                    sa.Column("target", sa.String(64), nullable=False),
                    sa.Column("created_at", sa.DateTime(), nullable=False))
    op.create_index("ix_admin_audit_created_at", "admin_audit", ["created_at"])
    op.create_table("auth_sessions",
                    sa.Column("token_hash", sa.String(64), primary_key=True),
                    sa.Column("role", sa.String(16), nullable=False),
                    sa.Column("subject", sa.String(255), nullable=False),
                    sa.Column("identity_email", sa.String(255), nullable=False),
                    sa.Column("created_at", sa.DateTime(), nullable=False),
                    sa.Column("expires_at", sa.DateTime(), nullable=False),
                    sa.Column("revoked_at", sa.DateTime()))
    op.create_index("ix_auth_sessions_subject", "auth_sessions", ["subject"])
    op.create_index("ix_auth_sessions_expires_at", "auth_sessions", ["expires_at"])
    op.create_table("webhook_receipts",
                    sa.Column("fingerprint", sa.String(64), primary_key=True),
                    sa.Column("delivery_id", sa.String(128), nullable=False),
                    sa.Column("topic", sa.String(64), nullable=False),
                    sa.Column("processed_at", sa.DateTime(), nullable=False))
    op.create_table("gift_card_issuances",
                    sa.Column("return_id", sa.Integer(), sa.ForeignKey("return_requests.id"), primary_key=True),
                    sa.Column("code", sa.String(32), nullable=False, unique=True),
                    sa.Column("amount", sa.Numeric(10, 2), nullable=False),
                    sa.Column("state", sa.String(16), nullable=False),
                    sa.Column("provider_id", sa.String(64), unique=True),
                    sa.Column("requested_by", sa.String(255), nullable=False),
                    sa.Column("confirmed_by", sa.String(255)),
                    sa.Column("created_at", sa.DateTime(), nullable=False),
                    sa.Column("confirmed_at", sa.DateTime()))


def downgrade():
    op.drop_table("admin_audit")
    op.drop_table("gift_card_issuances")
    op.drop_table("webhook_receipts")
    op.drop_table("auth_sessions")
    op.drop_column("orders", "shopify_updated_at")
