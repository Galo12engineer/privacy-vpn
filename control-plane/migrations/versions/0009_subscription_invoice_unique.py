"""unique index on subscription.invoice_id (DB backstop vs double-credit on webhook race)

NULL is allowed many times over (the scrub breaks the bridge invoice_id→None); only ONE non-empty
invoice_id per subscription → a repeated credit for the same invoice is impossible at the DB level.

Revision ID: 0009
Revises: 0008
Create Date: 2026-07-03
"""
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "uq_subscription_invoice_id", "subscription", ["invoice_id"], unique=True
    )


def downgrade() -> None:
    op.drop_index("uq_subscription_invoice_id", table_name="subscription")
