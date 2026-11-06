"""price_plan.featured — the "recommended" plan (anchor on the landing page)

Revision ID: 0016
Revises: 0015
Create Date: 2026-07-09

Boolean flag: highlight the plan as recommended. Default false. The "no more than one
featured" invariant is enforced by admin_service (not the DB). Reversible.
"""
import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "price_plan",
        sa.Column("featured", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    with op.batch_alter_table("price_plan") as batch:
        batch.drop_column("featured")
