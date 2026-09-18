"""Admin OTP, request addresses, exchange photos and email queue.

Revision ID: 93c7e02a1b44
Revises: 066cf68194cc
"""
from alembic import op
import sqlalchemy as sa

revision = "93c7e02a1b44"
down_revision = "066cf68194cc"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("orders", sa.Column("shipping_address", sa.JSON(), nullable=True))
    op.add_column("return_requests", sa.Column("pickup_address", sa.JSON(), nullable=True))
    op.add_column("exchange_requests", sa.Column("pickup_address", sa.JSON(), nullable=True))
    op.add_column("exchange_requests", sa.Column("photo_urls", sa.JSON(), nullable=True))
    op.create_table("admin_otps",
        sa.Column("email", sa.String(255), primary_key=True),
        sa.Column("otp_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("consumed", sa.Boolean(), nullable=False))
    op.create_table("notifications",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("event_key", sa.String(100), unique=True, nullable=False),
        sa.Column("recipient", sa.String(255), nullable=False),
        sa.Column("subject", sa.String(255), nullable=False),
        sa.Column("html", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("sent_at", sa.DateTime()),
        sa.Column("attempts", sa.Integer(), nullable=False))


def downgrade():
    op.drop_table("notifications")
    op.drop_table("admin_otps")
    op.drop_column("exchange_requests", "photo_urls")
    op.drop_column("exchange_requests", "pickup_address")
    op.drop_column("return_requests", "pickup_address")
    op.drop_column("orders", "shipping_address")
