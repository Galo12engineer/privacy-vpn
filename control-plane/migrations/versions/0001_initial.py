"""initial schema: accounts, billing (time-credit), nodes, IPAM, peers

Revision ID: 0001
Revises:
Create Date: 2026-06-20
"""
from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "account",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("account_number_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="expired"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("device_limit", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("email_encrypted", sa.LargeBinary(), nullable=True),
        sa.Column("reminders_opt_in", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("peers_revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_account_account_number_hash", "account", ["account_number_hash"], unique=True)

    op.create_table(
        "price_plan",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("code", sa.String(32), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("duration_days", sa.Integer(), nullable=False),
        sa.Column("price_fiat_cents", sa.BigInteger(), nullable=False),
        sa.Column("fiat_currency", sa.String(3), nullable=False, server_default="USD"),
        sa.Column("device_limit", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_price_plan_code", "price_plan", ["code"], unique=True)

    op.create_table(
        "node",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("public_key", sa.String(64), nullable=True),
        sa.Column("endpoint_host", sa.String(255), nullable=False),
        sa.Column("endpoint_port", sa.Integer(), nullable=False, server_default="51820"),
        sa.Column("endpoint_port_alt", sa.Integer(), nullable=False, server_default="443"),
        sa.Column("location_code", sa.String(16), nullable=False),
        sa.Column("location_name", sa.String(120), nullable=False),
        sa.Column("subnet_v4", sa.String(43), nullable=False),
        sa.Column("subnet_v6", sa.String(43), nullable=True),
        sa.Column("dns_ip", sa.String(45), nullable=False),
        sa.Column("mtu", sa.Integer(), nullable=False, server_default="1420"),
        sa.Column("status", sa.String(16), nullable=False, server_default="provisioning"),
        sa.Column("available_for_sale", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("mtls_cert_fingerprint", sa.String(95), nullable=True),
        sa.Column("last_health_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("health_cpu_pct", sa.Integer(), nullable=True),
        sa.Column("health_mem_pct", sa.Integer(), nullable=True),
        sa.Column("health_peers", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_node_name", "node", ["name"], unique=True)

    op.create_table(
        "invoice",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("account_id", sa.Uuid(), sa.ForeignKey("account.id", ondelete="SET NULL"), nullable=True),
        sa.Column("price_plan_id", sa.Uuid(), sa.ForeignKey("price_plan.id", ondelete="SET NULL"), nullable=True),
        sa.Column("method", sa.String(16), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="created"),
        sa.Column("days_granted", sa.Integer(), nullable=False),
        sa.Column("amount_fiat_cents", sa.BigInteger(), nullable=False),
        sa.Column("fiat_currency", sa.String(3), nullable=False, server_default="USD"),
        sa.Column("crypto_currency", sa.String(8), nullable=True),
        sa.Column("crypto_amount", sa.Numeric(30, 12), nullable=True),
        sa.Column("exchange_rate", sa.Numeric(30, 12), nullable=True),
        sa.Column("rate_locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rate_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("external_id", sa.String(255), nullable=True),
        sa.Column("payment_address", sa.String(255), nullable=True),
        sa.Column("confirmations_required", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("confirmations_seen", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("scrubbed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_invoice_account_id", "invoice", ["account_id"])
    op.create_index("ix_invoice_status", "invoice", ["status"])
    op.create_index("ix_invoice_external_id", "invoice", ["external_id"])

    op.create_table(
        "subscription",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("account_id", sa.Uuid(), sa.ForeignKey("account.id", ondelete="CASCADE"), nullable=False),
        sa.Column("invoice_id", sa.Uuid(), sa.ForeignKey("invoice.id", ondelete="SET NULL"), nullable=True),
        sa.Column("price_plan_code", sa.String(32), nullable=True),
        sa.Column("days", sa.Integer(), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_subscription_account_id", "subscription", ["account_id"])

    op.create_table(
        "ip_lease",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("node_id", sa.Uuid(), sa.ForeignKey("node.id", ondelete="CASCADE"), nullable=False),
        sa.Column("address", sa.String(45), nullable=False),
        sa.Column("address_v6", sa.String(45), nullable=True),
        sa.Column("allocated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("node_id", "address", name="uq_lease_node_address"),
    )
    op.create_index("ix_ip_lease_node_id", "ip_lease", ["node_id"])

    op.create_table(
        "peer",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("account_id", sa.Uuid(), sa.ForeignKey("account.id", ondelete="CASCADE"), nullable=False),
        sa.Column("node_id", sa.Uuid(), sa.ForeignKey("node.id", ondelete="CASCADE"), nullable=False),
        sa.Column("ip_lease_id", sa.Uuid(), sa.ForeignKey("ip_lease.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("public_key", sa.String(64), nullable=False),
        sa.Column("preshared_key", sa.String(64), nullable=True),
        sa.Column("name", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("node_id", "public_key", name="uq_peer_node_pubkey"),
    )
    op.create_index("ix_peer_account_id", "peer", ["account_id"])
    op.create_index("ix_peer_node_id", "peer", ["node_id"])

    op.create_table(
        "admin_user",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("username", sa.String(64), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("totp_secret_encrypted", sa.LargeBinary(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_admin_user_username", "admin_user", ["username"], unique=True)


def downgrade() -> None:
    op.drop_table("admin_user")
    op.drop_table("peer")
    op.drop_table("ip_lease")
    op.drop_table("subscription")
    op.drop_table("invoice")
    op.drop_table("node")
    op.drop_table("price_plan")
    op.drop_table("account")
