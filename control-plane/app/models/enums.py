from enum import StrEnum


class AccountStatus(StrEnum):
    ACTIVE = "active"
    GRACE = "grace"
    EXPIRED = "expired"


class NodeStatus(StrEnum):
    PROVISIONING = "provisioning"
    ACTIVE = "active"
    DRAINING = "draining"
    DOWN = "down"
    DISABLED = "disabled"


class NodeProtocol(StrEnum):
    """Data transport. wireguard — vanilla WG; amneziawg — obfuscated WG
    (junk packets + H1-H4 headers); vless_reality — VLESS over Reality+XHTTP (Xray),
    masquerading as genuine HTTPS. The value is used both on Node (the node's transport)
    and on Peer (the device's transport)."""

    WIREGUARD = "wireguard"
    AMNEZIAWG = "amneziawg"
    VLESS_REALITY = "vless_reality"


class InvoiceStatus(StrEnum):
    CREATED = "created"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    UNDERPAID = "underpaid"
    OVERPAID = "overpaid"
    PAID = "paid"
    EXPIRED = "expired"
    LATE_PAYMENT = "late_payment"
    CANCELLED = "cancelled"


class PaymentMethod(StrEnum):
    MONERO = "monero"
    BTC_ONCHAIN = "btc_onchain"
    LIGHTNING = "lightning"
    LITECOIN = "litecoin"
    USDT_TRC20 = "usdt_trc20"
    CASH = "cash"
