"""Private admin packing and unpacking evidence."""
from alembic import op
import sqlalchemy as sa

revision = "k52ab618e345"
down_revision = "j41fa507d234"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "order_videos",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("order_id", sa.Integer(), sa.ForeignKey("orders.id"), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("request_number", sa.String(20)),
        sa.Column("content_type", sa.String(32), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("created_by", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_order_videos_order_id", "order_videos", ["order_id"])


def downgrade():
    op.drop_table("order_videos")
