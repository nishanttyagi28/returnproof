from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from returnproof.config import get_settings
from sqlalchemy import create_engine, inspect


def test_alembic_upgrade_creates_all_tables(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "migrated.db"
    monkeypatch.setenv("RETURNPROOF_DATABASE_URL", f"sqlite:///{db_path}")
    get_settings.cache_clear()
    try:
        config = Config(str(Path("alembic.ini").resolve()))
        command.upgrade(config, "head")
        engine = create_engine(f"sqlite:///{db_path}")
        tables = set(inspect(engine).get_table_names())
        engine.dispose()
    finally:
        get_settings.cache_clear()

    expected = {
        "organisations",
        "users",
        "api_keys",
        "marketplaces",
        "products",
        "orders",
        "order_items",
        "shipments",
        "return_requests",
        "evidence",
        "verification_results",
        "claims",
        "settlements",
        "settlement_lines",
        "recovery_entries",
        "audit_events",
        "alembic_version",
    }
    assert expected <= tables
