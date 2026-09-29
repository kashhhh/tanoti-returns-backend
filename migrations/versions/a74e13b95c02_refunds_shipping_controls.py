"""Refund valuation evidence and durable carrier action history."""
from alembic import op
import sqlalchemy as sa

revision = "a74e13b95c02"
down_revision = "f63d029c471a"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("return_requests", sa.Column("refund_breakdown", sa.JSON()))
    op.add_column("return_requests", sa.Column("refund_review_error", sa.String(500)))
    for name, datatype in [
        ("carrier_status", sa.String(64)), ("carrier_status_code", sa.String(32)),
        ("tracking_checked_at", sa.DateTime()), ("tracking_error", sa.String(500)),
        ("action", sa.String(24)), ("action_state", sa.String(24)),
        ("action_requested_at", sa.DateTime()), ("action_error", sa.String(500)),
        ("action_history", sa.JSON()),
    ]:
        op.add_column("shipping_bookings", sa.Column(name, datatype))


def downgrade():
    for name in ("carrier_status", "carrier_status_code", "tracking_checked_at", "tracking_error",
                 "action", "action_state", "action_requested_at", "action_error", "action_history"):
        op.drop_column("shipping_bookings", name)
    op.drop_column("return_requests", "refund_review_error")
    op.drop_column("return_requests", "refund_breakdown")
