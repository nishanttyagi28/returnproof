from __future__ import annotations

from pathlib import Path

from returnproof.config import Settings
from returnproof.db import build_engine, build_session_factory, create_schema
from returnproof.demo import populate_demo
from returnproof.domain.models import Claim, Evidence, Organisation
from returnproof.seed import seed
from sqlalchemy import func, select


def test_demo_populates_evidence_and_claim(tmp_path: Path) -> None:
    settings = Settings(
        environment="test",
        database_url=f"sqlite:///{tmp_path / 'demo.db'}",
        upload_dir=tmp_path / "evidence",
        pack_dir=tmp_path / "packs",
        secret_key="demo-test",
    )
    engine = build_engine(settings.database_url)
    create_schema(engine)
    db = build_session_factory(engine)()
    try:
        seed(db)
        db.commit()
        organisation = db.scalar(select(Organisation).order_by(Organisation.id))
        assert organisation is not None
        stats = populate_demo(db, settings, organisation)
        assert stats["demo_email"] == "owner@nirvaanretail.example"
        assert stats["evidence_added"] >= 2
        assert stats["verifications"] >= 1
        assert db.scalar(select(func.count(Evidence.id))) >= 2
        assert db.scalar(select(func.count(Claim.id))) >= 1
        assert stats["claim_reference"]
        files = list((tmp_path / "evidence").rglob("*.png"))
        assert files
    finally:
        db.close()
        engine.dispose()
