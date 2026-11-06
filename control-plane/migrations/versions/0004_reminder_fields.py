"""account reminder timestamps

Revision ID: 0004
Revises: 0003
Create Date: 2026-06-20
"""
from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("account") as batch:
        batch.add_column(sa.Column("reminder_3d_sent_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("reminder_1d_sent_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("account") as batch:
        batch.drop_column("reminder_1d_sent_at")
        batch.drop_column("reminder_3d_sent_at")
