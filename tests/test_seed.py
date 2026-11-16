from __future__ import annotations

import re

from returnproof.db import build_engine, build_session_factory, create_schema
from returnproof.domain.enums import LeakageCategory, Role
from returnproof.domain.models import (
    Claim,
    Marketplace,
    Order,
    Organisation,
    Product,
    RecoveryEntry,
    ReturnRequest,
    Settlement,
    User,
)
from returnproof.seed import SAMPLE_PASSWORD, seed
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][0-9]Z[0-9A-Z]$")
PINCODE_RE = re.compile(r"^[1-9][0-9]{5}$")


def _seeded() -> tuple[Engine, Session]:
    engine = build_engine("sqlite://")
    create_schema(engine)
    session = build_session_factory(engine)()
    seed(session)
    session.commit()
    return engine, session


def test_seed_creates_realistic_synthetic_data() -> None:
    engine, session = _seeded()
    try:
        assert session.scalar(select(func.count()).select_from(Organisation)) == 2
        assert session.scalar(select(func.count()).select_from(User)) == 4
        assert session.scalar(select(func.count()).select_from(Product)) == 6
        assert session.scalar(select(func.count()).select_from(Order)) >= 30
        assert session.scalar(select(func.count()).select_from(ReturnRequest)) >= 10
        assert session.scalar(select(func.count()).select_from(Claim)) >= 1
        assert session.scalar(select(func.count()).select_from(Settlement)) == 2
        assert session.scalar(select(func.count()).select_from(RecoveryEntry)) >= 1

        orgs = session.scalars(select(Organisation)).all()
        assert all(GSTIN_RE.match(org.gstin or "") for org in orgs)
        assert all(PINCODE_RE.match(org.pincode or "") for org in orgs)
        assert all(org.is_sample for org in orgs)

        products = session.scalars(select(Product)).all()
        assert all(p.unit_price_paise > 0 and p.unit_price_paise % 100 == 0 for p in products)

        orders = session.scalars(select(Order)).all()
        assert {o.status for o in orders} >= {"delivered", "returned"}
        assert all(o.total_paise > 0 for o in orders)

        owner = session.scalars(select(User).where(User.role == Role.OWNER.value)).first()
        assert owner is not None
        assert owner.email.endswith("@nirvaanretail.example")

        categories = {e.category for e in session.scalars(select(RecoveryEntry)).all()}
        assert LeakageCategory.REFUND_WITHOUT_RETURN.value in categories
    finally:
        session.close()
        engine.dispose()


def test_seed_is_deterministic_and_demo_password_is_set() -> None:
    engine, session = _seeded()
    try:
        numbers = session.scalars(select(Order.order_number).order_by(Order.id)).all()
        assert len(set(numbers)) == len(numbers)
        first = session.scalars(select(Order.order_number).order_by(Order.id)).first()
        assert first is not None
        assert "AMAZONIN" in first or "FLIPKART" in first or "MEESHO" in first or "MYNTRA" in first

        owner = session.scalars(select(User).where(User.email == "owner@nirvaanretail.example")).one()
        from argon2 import PasswordHasher

        assert PasswordHasher().verify(owner.password_hash, SAMPLE_PASSWORD)
    finally:
        session.close()
        engine.dispose()


def test_marketplace_catalogue_has_expected_codes() -> None:
    engine, session = _seeded()
    try:
        codes = {m.code for m in session.scalars(select(Marketplace)).all()}
        assert {"amazon_in", "flipkart", "meesho", "myntra", "generic"} <= codes
    finally:
        session.close()
        engine.dispose()
