"""gift_token — prepaid access codes issued in batches, redeemed once each

Revision ID: 0019
Revises: 0018
Create Date: 2026-08-07

One new table. `token_hash` is UNIQUE — the token itself is never stored, only its HMAC
(services/gift.hash_token). `redeemed_at` is the one-shot marker taken under SELECT ... FOR UPDATE;
there is deliberately no account_id (app/models/gift_token.py).
"""
import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "gift_token",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("days", sa.Integer(), nullable=False),
        sa.Column("device_limit", sa.Integer(), nullable=True),
        sa.Column("price_plan_code", sa.String(length=32), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("redeemed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("note", sa.String(length=200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_gift_token_token_hash", "gift_token", ["token_hash"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_gift_token_token_hash", table_name="gift_token")
    op.drop_table("gift_token")
