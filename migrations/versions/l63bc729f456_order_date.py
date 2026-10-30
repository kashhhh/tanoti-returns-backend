"""Store the original Shopify order date separately from import time."""
from alembic import op
import sqlalchemy as sa
revision = "l63bc729f456"
down_revision = "k52ab618e345"
branch_labels = None
depends_on = None

def upgrade():
    op.add_column("orders", sa.Column("shopify_created_at", sa.DateTime(), nullable=True))

def downgrade():
    op.drop_column("orders", "shopify_created_at")
