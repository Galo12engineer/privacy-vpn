"""#30 drop the dead peer.revoked_at column (never assigned; all paths are hard-delete)

Revision ID: 0012
Revises: 0011
Create Date: 2026-07-06

The column was always NULL, while 3 hot queries filtered on `revoked_at IS NULL` (dead
predicates) — plus a latent trap: uq_peer_node_pubkey is incompatible with a future soft-delete.
Under no-logs the hard-delete model is the correct one → we remove the column and the predicates.
Fully reversible (downgrade brings back an empty nullable column, there was no data).
"""
import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("peer") as batch:
        batch.drop_column("revoked_at")


def downgrade() -> None:
    with op.batch_alter_table("peer") as batch:
        batch.add_column(sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True))
