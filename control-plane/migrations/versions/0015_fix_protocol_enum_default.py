"""Fix node.protocol / peer.protocol_type enum encoding: NAME, not value

Revision ID: 0015
Revises: 0014
Create Date: 2026-07-09

The model stores protocol/protocol_type as SAEnum(NodeProtocol, native_enum=False) — SQLAlchemy
encodes such enums by member NAME ('WIREGUARD'), while migrations 0013/0014 set the server_default
by VALUE ('wireguard'). A row backfilled with that default (a node that existed before 0013)
could not be read by the model → LookupError on hydration (this broke node_health, /admin/nodes,
/devices/add). Found by a live deploy on 2026-07-09.

We bring existing values to the NAME encoding and fix the server_default. Idempotent
(rows already in the NAME encoding are left untouched). Reversible.
"""
import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

# value (as server_default erroneously wrote it) -> NAME (as SAEnum native_enum=False stores it)
_MAP = {
    "wireguard": "WIREGUARD",
    "amneziawg": "AMNEZIAWG",
    "vless_reality": "VLESS_REALITY",
}


def upgrade() -> None:
    for value, name in _MAP.items():
        op.execute(f"UPDATE node SET protocol='{name}' WHERE protocol='{value}'")
        op.execute(f"UPDATE peer SET protocol_type='{name}' WHERE protocol_type='{value}'")
    with op.batch_alter_table("node") as batch:
        batch.alter_column("protocol", existing_type=sa.String(16), server_default="WIREGUARD")
    with op.batch_alter_table("peer") as batch:
        batch.alter_column("protocol_type", existing_type=sa.String(16), server_default="WIREGUARD")


def downgrade() -> None:
    with op.batch_alter_table("peer") as batch:
        batch.alter_column("protocol_type", existing_type=sa.String(16), server_default="wireguard")
    with op.batch_alter_table("node") as batch:
        batch.alter_column("protocol", existing_type=sa.String(16), server_default="wireguard")
    for value, name in _MAP.items():
        op.execute(f"UPDATE node SET protocol='{value}' WHERE protocol='{name}'")
        op.execute(f"UPDATE peer SET protocol_type='{value}' WHERE protocol_type='{name}'")
