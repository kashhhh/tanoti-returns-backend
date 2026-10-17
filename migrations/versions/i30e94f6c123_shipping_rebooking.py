"""Preserve each Delhivery booking when a cancelled shipment is rebooked."""
from alembic import op
import sqlalchemy as sa

revision = "i30e94f6c123"
down_revision = "h29d83e5b012"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("shipping_bookings") as batch:
        batch.add_column(sa.Column("attempt", sa.Integer(), nullable=False, server_default="1"))
        batch.drop_constraint("uq_shipping_request_leg", type_="unique")
        batch.create_unique_constraint("uq_shipping_request_leg_attempt", ["request_number", "leg", "attempt"])


def downgrade():
    if op.get_bind().execute(sa.text("SELECT COUNT(*) FROM shipping_bookings WHERE attempt > 1")).scalar():
        raise RuntimeError("Cannot downgrade while rebooking history exists; preserve the booking ledger.")
    with op.batch_alter_table("shipping_bookings") as batch:
        batch.drop_constraint("uq_shipping_request_leg_attempt", type_="unique")
        batch.drop_column("attempt")
        batch.create_unique_constraint("uq_shipping_request_leg", ["request_number", "leg"])
