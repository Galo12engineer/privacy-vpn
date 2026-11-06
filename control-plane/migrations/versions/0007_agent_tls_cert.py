"""agent self-signed TLS cert (pinned control-channel, H1)

Revision ID: 0007
Revises: 0006
Create Date: 2026-06-29
"""
from alembic import op
import sqlalchemy as sa

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("node") as batch:
        batch.add_column(sa.Column("agent_tls_cert", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("node") as batch:
        batch.drop_column("agent_tls_cert")
