from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Control-plane configuration. All values come from env with the CP_ prefix."""

    model_config = SettingsConfigDict(env_prefix="CP_", env_file=".env", extra="ignore")

    database_url: str = "sqlite+pysqlite:///./control_plane.db"

    # Pepper for the HMAC account-number — in the secret-store, not in the DB/repo
    account_number_pepper: str = "dev-only-insecure-pepper-change-me"
    # Fernet key for encrypting the optional email
    email_encryption_key: str | None = None

    # Subscription lifecycle
    grace_period_days: int = 3
    default_device_limit: int = 5

    # Billing
    invoice_ttl_minutes: int = 60
    rate_lock_window_minutes: int = 15
    btc_confirmations_required: int = 2
    xmr_confirmations_required: int = 10
    allow_zero_conf: bool = False

    # Retention window for the invoice↔account link after activation (minutes) until scrubbing
    invoice_link_retention_minutes: int = 0
    # Retention window for the link on TERMINALLY-UNPAID invoices (EXPIRED/LATE_PAYMENT/CANCELLED)
    # until scrubbing: we keep it for reconcile/refund of a possible unaccounted incoming payment,
    # then clean up — we don't store account↔deposit-address forever (no-logs). Default 30 days.
    invoice_unpaid_retention_minutes: int = 43200
    # How long reconcile keeps AUTO-polling a late_payment invoice (days). Expiry now routes an
    # invoice whose deposit address was live into late_payment (so late coins auto-credit) — but
    # polling every abandoned-but-addressed invoice for the full 30-day retention would hammer the
    # gateway for nothing: a late on-chain payment realistically lands within hours, not weeks.
    # Past this window the linkage is still retained for MANUAL rescue via the admin extend page.
    reconcile_late_max_age_days: int = 7

    # CP → node-agent (control channel). mTLS on the transport + HMAC signature of the body.
    agent_control_scheme: str = "https"
    agent_control_port: int = 8443
    agent_ca_bundle: str | None = None  # path to the CA for mTLS verification; None = system

    # --- Payments (secrets in the secret-store, NOT in the repo). BlockBee is the ONLY provider
    #     and it is non-custodial: funds arrive at the operator's wallets, CP is never a custodian.
    #     The NOWPayments / OxaPay / BTCPay / Monero adapters and their settings were removed
    #     2026-07-24 — none served a live payment method. Stale CP_GATEWAY_* / CP_OXAPAY_* /
    #     CP_BTCPAY_* / CP_MONERO_* env vars are now simply ignored (extra="ignore"). ---
    # BlockBee — non-custodial "address per order" + GET callback (no HMAC).
    # The primary gateway for BTC/LTC/USDT-TRC20: issues an address_in for the invoice, forwards to
    # the operator's payout. Callback authentication — a secret nonce in the callback URL.
    # See app/payments/blockbee.py.
    blockbee_enabled: bool = False
    blockbee_base_url: str = "https://api.blockbee.io"
    blockbee_api_key: str | None = None
    blockbee_callback_secret: str | None = None
    # operator's non-custodial payout addresses (BlockBee forwards funds directly here).
    # Keys = the coin strings from purchase._COIN: BTC / LTC / USDT-TRC20.
    blockbee_payout_btc: str | None = None
    blockbee_payout_ltc: str | None = None
    blockbee_payout_usdt_trc20: str | None = None
    # Public base URL of the CP (for the gateway's callback/return), e.g. https://privacyvpn.example.com
    public_base_url: str | None = None
    # DEV-ONLY: fake payment provider for local purchase testing (payment simulation).
    # NEVER enable in prod.
    dev_fake_payments: bool = False
    # Are sales open? false → /buy shows "payment coming soon", POST is blocked.
    # Default FALSE (fail-safe): open explicitly once the plans are set up and a provider is
    # configured — otherwise a clean deploy would open sales without being able to accept
    # payment (see assert_secrets_safe).
    purchases_open: bool = False
    # /guides/ru-vless — the RU bypass guide (VLESS+Reality). Default FALSE: the transport is not
    # sellable until a live RU test (docs/ru-validation.md §4), and advertising it early is exactly
    # the overclaim rule 6 of CLAUDE.md forbids. This flag replaces a hand-edit that used to be
    # required on EVERY deploy of cabinet.py — a manual step with no automated backstop, which is
    # how the page nearly shipped. Turn it on only when the RU test gate is actually cleared.
    guides_ru_vless_enabled: bool = False
    # operator details for the legal pages (/aup, /privacy). Filled in upon incorporation.
    legal_entity: str | None = None
    legal_jurisdiction: str | None = None
    legal_pgp_fingerprint: str | None = None
    # confirmations per coin (on-chain); Lightning — instant
    ltc_confirmations_required: int = 6
    usdt_confirmations_required: int = 20
    # underpayment tolerance (fraction), e.g. 0.01 = 1%
    payment_underpaid_tolerance: str = "0.01"

    # --- Scheduler / lifecycle ---
    scheduler_interval_seconds: int = 300
    # Two thresholds on purpose — silence means different things to an operator and to a customer.
    # node_stale_seconds: when to WAKE THE OPERATOR (monitor.sh reads this one). Raised 150 -> 300:
    #   transient transit packet loss / DNS blips on exit nodes (like ro-1) caused false alarms
    #   within a 2.5-min window. 300s allows a couple of retry cycles while still alerting fast.
    # node_offsale_seconds: when to PULL THE LOCATION FROM SALE. Deliberately looser: the node→CP
    #   path drops packets in transit, and on 29.07 ro-1 accumulated 14 silences over 150s in five
    #   days while the tunnels themselves never faltered — WireGuard is client↔node and does not
    #   touch this path. Taking a location off /buy over a few lost packets cost real sales silently
    #   (nothing in the logs said why the choice shrank). One threshold for both jobs meant the
    #   stricter one always won.
    node_stale_seconds: int = 300
    node_offsale_seconds: int = 600
    # PII retention (#26): an account that expired > N days ago loses its opt-in email/reminder
    # markers — we don't keep the encrypted email of dead accounts indefinitely. The number hash
    # remains (a returning user is not blocked). 0 = disabled. Default 90 days.
    account_retention_days: int = 90

    # --- Email (opt-in reminders; we minimize PII, Fernet at-rest) ---
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_from: str | None = None
    smtp_use_tls: bool = True

    # --- Cabinet / sessions / anti-brute-force ---
    session_secret: str = "dev-only-insecure-session-secret-change-me"
    session_ttl_seconds: int = 86400
    # The admin panel — shorter (M5): a smaller window for a stolen token. Plus the token is bound
    # to the password_hash stamp → changing the password revokes all admin sessions.
    admin_session_ttl_seconds: int = 7200  # 2 hours
    cookie_secure: bool = True  # turn off on .onion-only without TLS termination
    login_rate_max: int = 5      # attempts
    login_rate_window: int = 60  # over how many seconds
    # 2FA mandatory for the admin panel: True → an admin without TOTP cannot log in (first enroll
    # via scripts/enroll_admin_totp.py). Default False (fail-safe against lockout) — the operator
    # flips it AFTER enrollment. An admin with TOTP is always required to enter the code
    # (regardless of the flag).
    admin_require_2fa: bool = False
    # Secret admin path (defense-in-depth against brute-force/scanners): by default
    # /admin, but in prod the operator sets a high-entropy path (e.g.
    # CP_ADMIN_PATH=/c-9f3a1b7e2d4c8; generate with: `openssl rand -hex 8`). Then /admin/*
    # returns 404, and the panel lives under the secret. Docs/OpenAPI are disabled in prod → the
    # path does not leak. This is a layer ON TOP OF 2FA+rate-limit, not a replacement for them.
    admin_path: str = "/admin"

    @field_validator("admin_path")
    @classmethod
    def _normalize_admin_path(cls, v: str) -> str:
        """Normalize to the form '/segment[/segment...]' without a trailing slash; empty/'/' → /admin."""
        v = (v or "").strip()
        inner = v.strip("/")
        return "/" + inner if inner else "/admin"

    # Tor-only access to the admin panel: true → admin routes are visible only over .onion (Host
    # ends with .onion), from the clearnet (privacyvpn.example.com/.si via Caddy) → 404.
    # It works because .onion goes to the control-plane DIRECTLY (torrc HiddenServicePort),
    # while the clearnet goes through Caddy with the domain's Host; you cannot spoof the Host from
    # the clearnet (Caddy proxies only its own sites). Escape hatch against lockout: set it to false
    # over SSH + restart the control-plane (without redeploying code). Default FALSE (fail-safe).
    admin_onion_only: bool = False

    # fail-closed: outside dev mode we forbid default secrets (see assert_secrets_safe).
    # CP_DEV_MODE=true removes the guard for local dev/tests.
    dev_mode: bool = False

    # --- Attribution (UTM → cabinet → postback to Keitaro on activation) ---
    keitaro_postback_url: str | None = None

    # --- Public hosts, in priority order (first = primary). Whitelist for canonical/OpenGraph/
    #     sitemap URLs: the Host header is attacker-controlled, and echoing it into
    #     <link rel="canonical"> lets a stranger declare their URL canonical for our pages.
    #     An unknown Host degrades to the first entry. The .onion MUST stay listed — dropping it
    #     makes onion pages emit clearnet URLs for og:image, i.e. an external resource on the one
    #     surface where "zero external resources" is a product invariant. See app/seo.py. ---
    public_hosts: str = (
        "privacyvpn.example.com,privacyvpn.example.net,"
        "privacyvpnexampleonionaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.onion"
    )

    # --- Public support channels (/support). Empty → the channel is NOT rendered (no dead
    #     links): you enable them as they become ready via .env. PGP is taken from
    #     legal_pgp_fingerprint. ---
    support_telegram_url: str | None = None  # public group/channel, e.g. https://t.me/...
    support_telegram_dm: str | None = None   # @handle for direct messages
    support_x_url: str | None = None         # X/Twitter profile
    support_telegram_bot: str | None = None  # private support relay bot, e.g. https://t.me/privacyvpn_support_bot

    def assert_secrets_safe(self) -> None:
        """Fail-closed: forbid startup in prod with default secrets (C1) and
        dangerous dev flags (M6).

        Called on import of app.main. CP_DEV_MODE=true removes the check.
        """
        if self.dev_mode:
            return
        insecure = [
            name
            for name, val in (
                ("CP_SESSION_SECRET", self.session_secret),
                ("CP_ACCOUNT_NUMBER_PEPPER", self.account_number_pepper),
            )
            if "dev-only-insecure" in val
        ]
        if insecure:
            raise RuntimeError(
                "Refusing to start: default secrets — "
                + ", ".join(insecure)
                + ". Set them through the env (see scripts/gen-secrets.sh) "
                + "or set CP_DEV_MODE=true for local development."
            )
        # Ops: the default DB password (compose default POSTGRES_PASSWORD=changeme) must not
        # make it to prod — assert_secrets_safe did not check it before (a hole from the
        # static audit).
        if ":changeme@" in self.database_url:
            raise RuntimeError(
                "Refusing to start: the default DB password (POSTGRES_PASSWORD=changeme) is in "
                "CP_DATABASE_URL. Set a strong POSTGRES_PASSWORD in .env."
            )
        # M6: fake payments (free activation of any invoice via /dev/pay) are allowed
        # ONLY in dev mode. In prod — fatal (protection against accidental enabling).
        if self.dev_fake_payments:
            raise RuntimeError(
                "Refusing to start: CP_DEV_FAKE_PAYMENTS=true outside dev mode "
                "(any invoice activates for free). Turn the flag off "
                "or set CP_DEV_MODE=true for local use only."
            )
        # Sales are open, but payment acceptance is not configured → not allowed (the user would
        # create an invoice with no way to pay it). BlockBee is the only provider left, so this is
        # now a hard requirement rather than "at least one of two".
        if self.purchases_open:
            bb_ok = bool(
                self.blockbee_enabled
                and self.blockbee_api_key
                and self.blockbee_callback_secret
                and (
                    self.blockbee_payout_btc
                    or self.blockbee_payout_ltc
                    or self.blockbee_payout_usdt_trc20
                )
            )
            if not bb_ok:
                raise RuntimeError(
                    "Refusing to start: CP_PURCHASES_OPEN=true, but BlockBee is not configured "
                    "(needs CP_BLOCKBEE_ENABLED, the key, the callback secret and at least one payout "
                    "address). Sales must not open without a way to accept payment — configure the "
                    "provider or close sales."
                )


@lru_cache
def get_settings() -> Settings:
    return Settings()
