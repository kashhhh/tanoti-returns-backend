"""Durable Delhivery shipment attempts."""
from alembic import op
import sqlalchemy as sa

revision = "f63d029c471a"
down_revision = "e51a93bc820d"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("shipping_bookings",
        sa.Column("reference", sa.String(48), primary_key=True),
        sa.Column("request_number", sa.String(20), nullable=False),
        sa.Column("leg", sa.String(16), nullable=False),
        sa.Column("environment", sa.String(16), nullable=False),
        sa.Column("warehouse", sa.String(255), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("waybill", sa.String(64), unique=True),
        sa.Column("error", sa.String(500)),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("request_number", "leg", name="uq_shipping_request_leg"))
    op.create_index("ix_shipping_bookings_request_number", "shipping_bookings", ["request_number"])


def downgrade():
    op.drop_table("shipping_bookings")
