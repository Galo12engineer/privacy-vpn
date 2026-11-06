"""invoice.crypto_txs — accumulate a split payment instead of overwriting it

Revision ID: 0017
Revises: 0016
Create Date: 2026-07-18

The gateway reports the received value PER TRANSACTION. The code assigned that per-tx value to
crypto_amount_paid, so a payment split across two transactions overwrote the first part with the
second: the invoice stayed UNDERPAID while the customer had sent the full amount, and the poll could
not rescue it either (it compared the same per-tx value). This column holds {txid: "amount"} so the
running total is a SUM over unique transaction ids.

JSON, nullable, no server_default: an absent value simply means "no transactions recorded yet", and
existing rows keep working untouched. Linkage data (a txid ties an on-chain transaction to the
account), therefore it is nulled by scrub_invoice_linkage alongside address/amount. Reversible.
"""
import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("invoice", sa.Column("crypto_txs", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("invoice") as batch:
        batch.drop_column("crypto_txs")
