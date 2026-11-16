"""Database engine, session factory and request-scoped session dependency."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

from fastapi import Request
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from returnproof.config import Settings


def _ensure_sqlite_parent(database_url: str) -> None:
    prefix = "sqlite:///"
    if not database_url.startswith(prefix):
        return
    raw = database_url[len(prefix) :]
    if raw in {"", ":memory:"}:
        return
    Path(raw).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)


def build_engine(database_url: str, *, echo: bool = False) -> Engine:
    """Create a SQLAlchemy engine suitable for SQLite now and Postgres later."""
    _ensure_sqlite_parent(database_url)
    kwargs: dict[str, Any] = {"echo": echo, "future": True, "pool_pre_ping": True}
    if database_url.endswith(":memory:"):
        kwargs.update(
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
    elif database_url.startswith("sqlite"):
        kwargs.update(connect_args={"check_same_thread": False})
    engine = create_engine(database_url, **kwargs)
    if database_url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _set_sqlite_pragma(dbapi_connection: Any, _record: Any) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

    return engine


def build_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db(request: Request) -> Iterator[Session]:
    """FastAPI dependency yielding a request-scoped database session."""
    factory: sessionmaker[Session] = request.app.state.session_factory
    session = factory()
    try:
        yield session
    finally:
        session.close()


def create_schema(engine: Engine) -> None:
    """Create all tables directly; used by tests and local bootstrap."""
    from returnproof.domain.models import Base

    Base.metadata.create_all(engine)


def session_for(settings: Settings) -> Session:
    """Convenience for scripts: open a session against configured storage."""
    engine = build_engine(settings.database_url)
    return build_session_factory(engine)()
