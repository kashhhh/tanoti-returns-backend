"""Optional customer feedback on return and exchange requests.

Revision ID: d291c04fa832
Revises: a128e47f01cd
"""
from alembic import op
import sqlalchemy as sa

revision = "d291c04fa832"
down_revision = "a128e47f01cd"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("return_requests", sa.Column("customer_note", sa.Text(), nullable=True))
    op.add_column("exchange_requests", sa.Column("customer_note", sa.Text(), nullable=True))


def downgrade():
    op.drop_column("exchange_requests", "customer_note")
    op.drop_column("return_requests", "customer_note")
