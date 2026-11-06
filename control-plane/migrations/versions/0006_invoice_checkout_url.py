"""invoice hosted checkout url (gateway-checkout, e.g. OxaPay)

Revision ID: 0006
Revises: 0005
Create Date: 2026-06-24
"""
from alembic import op
import sqlalchemy as sa

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("invoice") as batch:
        batch.add_column(sa.Column("checkout_url", sa.String(length=512), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("invoice") as batch:
        batch.drop_column("checkout_url")
