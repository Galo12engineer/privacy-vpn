from collections.abc import Iterator

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings


def make_engine(url: str | None = None) -> Engine:
    """Engine factory. SQLite (dev) and PostgreSQL (prod) from a single place."""
    url = url or get_settings().database_url
    connect_args: dict = {}
    kwargs: dict = {}
    if url.startswith("sqlite"):
        from sqlalchemy.pool import StaticPool

        connect_args["check_same_thread"] = False
        if ":memory:" in url:
            kwargs["poolclass"] = StaticPool
    else:
        # Postgres: the long-lived scheduler holds the connection between 300s cycles → a DB
        # restart/backup/idle-drop leaves a dead connection (the next query fails with
        # OperationalError, the whole cycle is skipped). pre_ping discards dead ones
        # transparently; recycle < idle timeouts.
        kwargs["pool_pre_ping"] = True
        kwargs["pool_recycle"] = 1800
    return create_engine(url, future=True, connect_args=connect_args, **kwargs)


engine = make_engine()
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


def get_session() -> Iterator[Session]:
    """FastAPI dependency: one session per request."""
    with SessionLocal() as session:
        yield session
