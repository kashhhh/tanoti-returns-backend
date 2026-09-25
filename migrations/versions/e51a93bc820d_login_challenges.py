"""Opaque login challenges keep order email addresses private."""
from alembic import op
import sqlalchemy as sa

revision = "e51a93bc820d"
down_revision = "c94a8e2fd061"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("otp_tokens", sa.Column("challenge", sa.String(43), nullable=True))
    op.create_index("ix_otp_tokens_challenge", "otp_tokens", ["challenge"], unique=True)


def downgrade():
    op.drop_index("ix_otp_tokens_challenge", table_name="otp_tokens")
    op.drop_column("otp_tokens", "challenge")
