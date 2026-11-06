"""account attribution fields (UTM → Keitaro postback)

Revision ID: 0005
Revises: 0004
Create Date: 2026-06-20
"""
from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("account") as batch:
        batch.add_column(sa.Column("attribution_click_id", sa.String(64), nullable=True))
        batch.add_column(
            sa.Column("attribution_fired", sa.Boolean(), nullable=False, server_default=sa.false())
        )


def downgrade() -> None:
    with op.batch_alter_table("account") as batch:
        batch.drop_column("attribution_fired")
        batch.drop_column("attribution_click_id")
