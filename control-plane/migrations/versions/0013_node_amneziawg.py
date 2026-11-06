"""Phase 1 AmneziaWG: node.protocol + obfuscation profile (Jc,Jmin,Jmax,S1,S2,H1-H4)

Revision ID: 0013
Revises: 0012
Create Date: 2026-07-07

Makes the node transport pluggable. Default 'wireguard' — backward compatibility
(existing nodes do not change behavior). The awg_* columns are nullable, filled in only
when protocol='amneziawg'. Fully reversible (downgrade removes all added columns).
"""
import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

_AWG_COLS = [
    "awg_jc", "awg_jmin", "awg_jmax", "awg_s1", "awg_s2",
    "awg_h1", "awg_h2", "awg_h3", "awg_h4",
]


def upgrade() -> None:
    with op.batch_alter_table("node") as batch:
        batch.add_column(
            sa.Column("protocol", sa.String(16), nullable=False, server_default="wireguard")
        )
        for col in _AWG_COLS:
            batch.add_column(sa.Column(col, sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("node") as batch:
        for col in _AWG_COLS:
            batch.drop_column(col)
        batch.drop_column("protocol")
