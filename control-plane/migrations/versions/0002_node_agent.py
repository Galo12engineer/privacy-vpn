"""node agent fields: registration_token_hash, agent_secret

Revision ID: 0002
Revises: 0001
Create Date: 2026-06-20
"""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("node") as batch:
        batch.add_column(sa.Column("registration_token_hash", sa.String(64), nullable=True))
        batch.add_column(sa.Column("agent_secret", sa.String(64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("node") as batch:
        batch.drop_column("agent_secret")
        batch.drop_column("registration_token_hash")
