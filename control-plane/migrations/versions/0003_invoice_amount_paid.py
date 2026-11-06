"""invoice.crypto_amount_paid (for underpaid/overpaid detection)

Revision ID: 0003
Revises: 0002
Create Date: 2026-06-20
"""
from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("invoice") as batch:
        batch.add_column(sa.Column("crypto_amount_paid", sa.Numeric(30, 12), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("invoice") as batch:
        batch.drop_column("crypto_amount_paid")
