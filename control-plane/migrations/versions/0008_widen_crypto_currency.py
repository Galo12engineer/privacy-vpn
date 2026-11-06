"""widen invoice.crypto_currency 8->16 (fits 'USDT-TRC20' = 10 chars)

Revision ID: 0008
Revises: 0007
Create Date: 2026-06-30
"""
from alembic import op
import sqlalchemy as sa

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("invoice") as batch:
        batch.alter_column(
            "crypto_currency",
            existing_type=sa.String(length=8),
            type_=sa.String(length=16),
            existing_nullable=True,
        )


def downgrade() -> None:
    with op.batch_alter_table("invoice") as batch:
        batch.alter_column(
            "crypto_currency",
            existing_type=sa.String(length=16),
            type_=sa.String(length=8),
            existing_nullable=True,
        )
