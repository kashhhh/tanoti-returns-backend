"""Individual purchased units, replacement lineage and Shopify sync tracking.
Revision ID: f482b17a61e0
Revises: d291c04fa832
"""
from alembic import op
import sqlalchemy as sa
revision = "f482b17a61e0"
down_revision = "d291c04fa832"
branch_labels = None
depends_on = None


def upgrade():
    for column in [sa.Column("unit_number", sa.Integer(), nullable=False, server_default="1"),
                   sa.Column("line_quantity", sa.Integer(), nullable=False, server_default="1"),
                   sa.Column("fulfilled_at", sa.DateTime()),
                   sa.Column("fulfillment_known", sa.Boolean(), nullable=False, server_default=sa.false()),
                   sa.Column("superseded", sa.Boolean(), nullable=False, server_default=sa.false()),
                   sa.Column("unavailable", sa.Boolean(), nullable=False, server_default=sa.false()),
                   sa.Column("request_claimed", sa.Boolean(), nullable=False, server_default=sa.false()),
                   sa.Column("replacement_for", sa.String(20)),
                   sa.Column("shopify_fulfillment_line_id", sa.String(100))]:
        op.add_column("order_items", column)
    op.create_index("uq_order_item_replacement", "order_items", ["replacement_for"], unique=True)
    for table in ("return_requests", "exchange_requests"):
        for column in [sa.Column("restock", sa.Boolean()), sa.Column("shopify_location_id", sa.String(100)), sa.Column("shopify_return_id", sa.String(100)),
                       sa.Column("shopify_sync_status", sa.String(32), nullable=False, server_default="pending"),
                       sa.Column("shopify_sync_error", sa.Text()), sa.Column("shopify_sync_stage", sa.String(32)),
                       sa.Column("shopify_create_attempted", sa.Boolean(), nullable=False, server_default=sa.false())]:
            op.add_column(table, column)
    op.add_column("exchange_requests", sa.Column("requested_variant_id", sa.String(64)))
    op.add_column("exchange_requests", sa.Column("replacement_line_id", sa.String(64)))
    bind = op.get_bind()
    items = sa.Table("order_items", sa.MetaData(), autoload_with=bind)
    # Preserve IDs and existing request associations on unit 1; split remaining units.
    rows = bind.execute(sa.select(items)).mappings().all()
    for row in rows:
        count = max(1, row["quantity"] or 1)
        bind.execute(items.update().where(items.c.id == row["id"]).values(quantity=1, line_quantity=count))
        for number in range(2, count + 1):
            values = dict(row)
            values.pop("id")
            values.update(quantity=1, unit_number=number, line_quantity=count)
            bind.execute(items.insert().values(**values))
    bind.execute(sa.text("UPDATE order_items SET request_claimed = true WHERE id IN (SELECT order_item_id FROM return_requests UNION SELECT order_item_id FROM exchange_requests)"))


def downgrade():
    # Collapsing units would erase request identity. Restore a pre-upgrade backup instead.
    raise RuntimeError("This data migration cannot be safely downgraded; restore a backup.")
