"""Regression for the 2026-07-09 live-deploy bug: server_default of enum columns MUST be the NAME.

The model stores protocol/protocol_type as SAEnum(native_enum=False) → SQLAlchemy encodes by
the member NAME ('WIREGUARD'). Migrations 0013/0014 mistakenly set server_default to the VALUE
('wireguard') → the backfilled row could not be read by the model (LookupError). 0015 fixes this.
"""
from __future__ import annotations

import pathlib

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from app.config import get_settings
from app.models import Node
from app.models.enums import NodeProtocol

_MIGRATIONS = pathlib.Path(__file__).resolve().parents[1] / "migrations"


def _cfg(url: str) -> Config:
    c = Config()
    c.set_main_option("script_location", str(_MIGRATIONS))
    c.set_main_option("sqlalchemy.url", url)
    return c


def _node_default(engine) -> str:
    """DDL text of the node.protocol column (in SQLite the DEFAULT is visible)."""
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT sql FROM sqlite_master WHERE type='table' AND name='node'")
        ).scalar()


def test_migrations_head_default_is_enum_name(tmp_path, monkeypatch):
    url = f"sqlite+pysqlite:///{tmp_path / 'm.db'}"
    monkeypatch.setenv("CP_DATABASE_URL", url)
    monkeypatch.setenv("CP_DEV_MODE", "true")
    get_settings.cache_clear()
    try:
        cfg = _cfg(url)
        command.upgrade(cfg, "head")
        eng = create_engine(url)
        ddl = _node_default(eng)
        # server_default in NAME encoding — otherwise the backfilled row is not readable by the model
        assert "'WIREGUARD'" in ddl, ddl
        assert "DEFAULT 'wireguard'" not in ddl, ddl

        # 0015 is reversible: down to 0014 restores the old (buggy) default, up fixes it again
        command.downgrade(cfg, "0014")
        assert "DEFAULT 'wireguard'" in _node_default(eng)
        command.upgrade(cfg, "head")
        assert "'WIREGUARD'" in _node_default(eng)
        eng.dispose()
    finally:
        get_settings.cache_clear()


def test_saenum_stores_protocol_by_name(session, node):
    """Encoding contract: the ORM writes protocol by NAME ('WIREGUARD'), not by value.
    If this is changed — the migration server_default must be changed in sync."""
    # a single node in the function-scoped test DB (node fixture); no WHERE by id, because SQLite
    # stores the UUID without dashes and a string match is not trivial
    raw = session.execute(text("SELECT protocol FROM node")).scalar()
    assert raw == "WIREGUARD"  # the member NAME, not the value 'wireguard'
    # and back: the model hydrates it into an enum without LookupError
    fresh = session.get(Node, node.id)
    assert fresh.protocol is NodeProtocol.WIREGUARD
