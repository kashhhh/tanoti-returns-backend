"""Restrict address-phone OTPs and sessions to one order."""
from alembic import op
import sqlalchemy as sa

revision = "j41fa507d234"
down_revision = "i30e94f6c123"
branch_labels = None
depends_on = None


def upgrade():
    for table in ("otp_tokens", "auth_sessions"):
        with op.batch_alter_table(table) as batch:
            batch.add_column(sa.Column("order_scope_id", sa.Integer(), nullable=True))
            batch.create_foreign_key("fk_" + table + "_order_scope", "orders", ["order_scope_id"], ["id"])


def downgrade():
    # Never turn a restricted credential into an unrestricted one on downgrade.
    op.execute("DELETE FROM auth_sessions WHERE order_scope_id IS NOT NULL")
    op.execute("DELETE FROM otp_tokens WHERE order_scope_id IS NOT NULL")
    for table in ("auth_sessions", "otp_tokens"):
        with op.batch_alter_table(table) as batch:
            batch.drop_constraint("fk_" + table + "_order_scope", type_="foreignkey")
            batch.drop_column("order_scope_id")
