"""promo_code + invoice.discount_percent — percent-off codes managed from the admin console

Revision ID: 0018
Revises: 0017
Create Date: 2026-07-20

Two changes, both additive and reversible:

1. `promo_code` — the codes themselves. `code` is stored normalized (upper-case, A-Z0-9), so the
   UNIQUE index actually prevents "launch20" and "LAUNCH-20" being two rows. `used_count` is the
   capacity counter; there is deliberately NO redemption ledger and no FK to invoice/account —
   that table would rebuild the code↔account↔time linkage that scrub_invoice_linkage exists to
   destroy (see app/models/promo_code.py).

2. `invoice.discount_percent` — the snapshot of the percent applied. NOT NULL with
   server_default='0': existing invoices were bought at list price, and 0 is the truthful value
   for them; the default also keeps a concurrent old-code writer (a control-plane container still
   running the previous image during a rolling deploy) from failing its INSERT.
   BigInteger is not needed — a percent fits in an int.
"""
import sqlalchemy as sa
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "promo_code",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("percent_off", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("max_uses", sa.Integer(), nullable=True),
        sa.Column("used_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("note", sa.String(length=200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_promo_code_code", "promo_code", ["code"], unique=True)
    # batch_alter_table, NOT a bare op.create_foreign_key: SQLite cannot ALTER a constraint into an
    # existing table ("No support for ALTER of constraints in SQLite dialect"), so a plain
    # create_foreign_key would leave dev/CI unable to migrate at all while Postgres was fine —
    # and the reverse drift (tests get the FK from metadata.create_all, prod does not) is the same
    # SQLite-vs-Postgres divergence that has bitten this repo before. In batch mode Postgres still
    # receives ordinary ALTER TABLE statements; only SQLite does the copy-and-move.
    with op.batch_alter_table("invoice") as batch:
        batch.add_column(
            sa.Column("discount_percent", sa.Integer(), nullable=False, server_default="0")
        )
        # Nullable + SET NULL: the payment-window linkage, wiped by scrub_invoice_linkage alongside
        # account_id. It exists so an invoice that dies unpaid can return the reservation it took
        # (services/promo.release) — see app/models/invoice.py.
        batch.add_column(sa.Column("promo_code_id", sa.Uuid(), nullable=True))
        batch.create_foreign_key(
            "fk_invoice_promo_code_id", "promo_code",
            ["promo_code_id"], ["id"], ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("invoice") as batch:
        batch.drop_constraint("fk_invoice_promo_code_id", type_="foreignkey")
        batch.drop_column("promo_code_id")
        batch.drop_column("discount_percent")
    op.drop_index("ix_promo_code_code", table_name="promo_code")
    op.drop_table("promo_code")
