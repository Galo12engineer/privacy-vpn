import os

# tests use default secrets → we lift the fail-closed guard (see app/config.py)
os.environ.setdefault("CP_DEV_MODE", "true")

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.models import Account, Base, Node
from app.models.enums import AccountStatus


@pytest.fixture()
def engine():
    eng = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def session(engine):
    with Session(engine, expire_on_commit=False) as s:
        yield s


@pytest.fixture()
def node(session):
    """Small /29 subnet: hosts .1-.6, .1 = server → 5 assignable addresses."""
    n = Node(
        name="pa-1",
        endpoint_host="pa1.example.net",
        location_code="pa",
        location_name="Panama",
        subnet_v4="10.7.0.0/29",
        dns_ip="10.7.0.1",
    )
    session.add(n)
    session.commit()
    return n


@pytest.fixture()
def node_v6(session):
    n = Node(
        name="is-1",
        endpoint_host="is1.example.net",
        location_code="is",
        location_name="Iceland",
        subnet_v4="10.8.0.0/29",
        subnet_v6="fd00:8::/64",
        dns_ip="10.8.0.1",
    )
    session.add(n)
    session.commit()
    return n


@pytest.fixture()
def price_plan(session):
    from app.models import PricePlan

    p = PricePlan(
        code="1m",
        name="1 month",
        duration_days=30,
        price_fiat_cents=500,
        fiat_currency="USD",
        device_limit=5,
    )
    session.add(p)
    session.commit()
    return p


@pytest.fixture()
def active_account(session):
    """Account with active time (+30 days)."""
    a = Account(
        account_number_hash="active".ljust(64, "0"),
        status=AccountStatus.ACTIVE,
        expires_at=datetime.now(UTC) + timedelta(days=30),
        device_limit=5,
    )
    session.add(a)
    session.commit()
    return a
