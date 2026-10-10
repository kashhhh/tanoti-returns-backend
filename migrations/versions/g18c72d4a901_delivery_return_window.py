"""Start five-day return windows at confirmed delivery."""
from alembic import op
import sqlalchemy as sa

revision = "g18c72d4a901"
down_revision = "a74e13b95c02"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("orders", sa.Column("delivered_at", sa.DateTime(), nullable=True))
    op.add_column("order_items", sa.Column("delivered_at", sa.DateTime(), nullable=True))
    # Original delivery dates must come from Shopify, never fulfillment dates.
    op.execute("UPDATE order_items SET delivered_at = (SELECT delivered_at FROM exchange_requests "
               "WHERE exchange_number = order_items.replacement_for) WHERE replacement_for IS NOT NULL")
    op.execute("UPDATE admin_settings SET return_window_days = 5")


def downgrade():
    op.drop_column("order_items", "delivered_at")
    op.drop_column("orders", "delivered_at")
