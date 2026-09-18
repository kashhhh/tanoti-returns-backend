"""Record actual manual refund payment and exchange delivery.

Revision ID: a128e47f01cd
Revises: 93c7e02a1b44
"""
from alembic import op
import sqlalchemy as sa
revision = "a128e47f01cd"
down_revision = "93c7e02a1b44"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("return_requests", sa.Column("refund_paid_at", sa.DateTime(), nullable=True))
    op.add_column("return_requests", sa.Column("refund_paid_by", sa.String(255), nullable=True))
    op.add_column("exchange_requests", sa.Column("delivered_at", sa.DateTime(), nullable=True))
    op.add_column("exchange_requests", sa.Column("delivered_by", sa.String(255), nullable=True))
    # Do not infer historic payments or deliveries from the old approval status.


def downgrade():
    op.drop_column("exchange_requests", "delivered_by")
    op.drop_column("exchange_requests", "delivered_at")
    op.drop_column("return_requests", "refund_paid_by")
    op.drop_column("return_requests", "refund_paid_at")
