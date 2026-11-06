"""Phase 2 VLESS+Reality: node.reality_config; peer.protocol_type + vless_uuid/email; public_key nullable

Revision ID: 0014
Revises: 0013
Create Date: 2026-07-07

Per-device transport (Peer.protocol_type): Xray/VLESS lives alongside WireGuard
on the same node. A VLESS peer has no WG key → public_key becomes nullable (NULLs in
uq_peer_node_pubkey do not conflict). reality_config — the server's JSON profile on the node.
Reversible (downgrade is correct on a DB without VLESS peers: public_key returns to NOT NULL).
"""
import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("node") as batch:
        batch.add_column(sa.Column("reality_config", sa.Text(), nullable=True))
    with op.batch_alter_table("peer") as batch:
        batch.add_column(
            sa.Column("protocol_type", sa.String(16), nullable=False, server_default="wireguard")
        )
        batch.add_column(sa.Column("vless_uuid", sa.String(36), nullable=True))
        batch.add_column(sa.Column("vless_email", sa.String(64), nullable=True))
        batch.alter_column("public_key", existing_type=sa.String(64), nullable=True)
        # a VLESS peer has no tunnel IP → a dedicated lease is not required
        batch.alter_column("ip_lease_id", existing_type=sa.Uuid(), nullable=True)
        batch.create_unique_constraint("uq_peer_node_vless_uuid", ["node_id", "vless_uuid"])


def downgrade() -> None:
    with op.batch_alter_table("peer") as batch:
        batch.drop_constraint("uq_peer_node_vless_uuid", type_="unique")
        batch.alter_column("ip_lease_id", existing_type=sa.Uuid(), nullable=False)
        batch.alter_column("public_key", existing_type=sa.String(64), nullable=False)
        batch.drop_column("vless_email")
        batch.drop_column("vless_uuid")
        batch.drop_column("protocol_type")
    with op.batch_alter_table("node") as batch:
        batch.drop_column("reality_config")
