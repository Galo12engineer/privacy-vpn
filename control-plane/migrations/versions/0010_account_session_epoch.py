"""account.session_epoch (#17): binding for cabinet sessions → "log out everywhere" = increment

Revision ID: 0010
Revises: 0009
Create Date: 2026-07-03
"""
import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "account",
        sa.Column("session_epoch", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("account", "session_epoch")
