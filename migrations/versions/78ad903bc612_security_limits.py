"""Shared security counters.

Revision ID: 78ad903bc612
Revises: f482b17a61e0
"""
from alembic import op
import sqlalchemy as sa

revision = "78ad903bc612"
down_revision = "f482b17a61e0"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("security_rate_limits",
                    sa.Column("key", sa.String(64), primary_key=True),
                    sa.Column("window", sa.BigInteger(), primary_key=True),
                    sa.Column("count", sa.Integer(), nullable=False),
                    sa.Column("expires_at", sa.BigInteger(), nullable=False))
    op.create_index("ix_security_rate_limits_expires_at", "security_rate_limits", ["expires_at"])


def downgrade():
    op.drop_table("security_rate_limits")
