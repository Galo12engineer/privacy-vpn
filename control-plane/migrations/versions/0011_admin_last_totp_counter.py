"""admin_user.last_totp_counter (#23): a TOTP code is single-use (anti-replay)

Revision ID: 0011
Revises: 0010
Create Date: 2026-07-03
"""
import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("admin_user", sa.Column("last_totp_counter", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("admin_user", "last_totp_counter")
