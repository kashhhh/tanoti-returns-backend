"""Track store-wide order imports and delivery freshness."""
from alembic import op
import sqlalchemy as sa

revision = "h29d83e5b012"
down_revision = "g18c72d4a901"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("orders", sa.Column("delivery_checked_at", sa.DateTime(), nullable=True))
    op.create_table("store_order_sync",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("started_at", sa.DateTime()), sa.Column("completed_at", sa.DateTime()),
        sa.Column("next_page", sa.Text()), sa.Column("running", sa.Boolean(), nullable=False),
        sa.Column("processed", sa.Integer(), nullable=False), sa.Column("skipped", sa.Integer(), nullable=False),
        sa.Column("error", sa.String(255)))


def downgrade():
    op.drop_table("store_order_sync")
    op.drop_column("orders", "delivery_checked_at")
