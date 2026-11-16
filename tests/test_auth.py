from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from returnproof.auth import generate_api_key, hash_password, verify_password
from returnproof.domain.enums import Role
from returnproof.domain.models import (
    ApiKey,
    Marketplace,
    Order,
    Organisation,
    User,
    utcnow,
)
from sqlalchemy.orm import Session

CSRF_RE = re.compile(r'name="csrf" value="([^"]+)"')


def _session(client: TestClient) -> Session:
    return client.app.state.session_factory()


def make_tenant(client: TestClient, *, slug: str, role: Role = Role.OWNER) -> tuple[Organisation, User]:
    db = _session(client)
    try:
        org = Organisation(
            name=f"{slug} Retail",
            legal_name=f"{slug} Retail Private Limited",
            gstin=None,
            email=f"ops@{slug}.example",
            city="Pune",
            state="Maharashtra",
            pincode="411001",
            is_sample=True,
        )
        db.add(org)
        db.flush()
        user = User(
            org_id=org.id,
            email=f"{role.value}@{slug}.example",
            password_hash=hash_password("CorrectHorse@2026"),
            full_name=f"{slug.title()} {role.value.title()}",
            role=role.value,
        )
        db.add(user)
        db.commit()
        return org, user
    finally:
        db.close()


def make_order(client: TestClient, org: Organisation, number: str) -> Order:
    db = _session(client)
    try:
        marketplace = db.query(Marketplace).filter_by(code="generic").one_or_none()
        if marketplace is None:
            marketplace = Marketplace(code="generic", name="Generic", claim_window_days=15)
            db.add(marketplace)
            db.flush()
        order = Order(
            org_id=org.id,
            marketplace_id=marketplace.id,
            order_number=number,
            placed_at=datetime.now(UTC) - timedelta(days=2),
            customer_name="Sample Buyer",
            customer_pincode="411001",
            customer_state="Maharashtra",
            status="delivered",
            total_paise=129900,
            is_sample=True,
        )
        db.add(order)
        db.commit()
        db.refresh(order)
        return order
    finally:
        db.close()


def make_api_key(client: TestClient, org: Organisation, user: User) -> str:
    token, prefix, key_hash = generate_api_key()
    db = _session(client)
    try:
        db.add(
            ApiKey(
                org_id=org.id,
                created_by_id=user.id,
                name="test key",
                prefix=prefix,
                key_hash=key_hash,
            )
        )
        db.commit()
    finally:
        db.close()
    return token


def login(client: TestClient, email: str, password: str = "CorrectHorse@2026") -> None:
    page = client.get("/login")
    csrf = CSRF_RE.search(page.text)
    assert csrf is not None
    response = client.post(
        "/login",
        data={"email": email, "password": password, "csrf": csrf.group(1)},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text


def test_password_hashing_roundtrip() -> None:
    hashed = hash_password("s3cret-passphrase")
    assert hashed.startswith("$argon2")
    assert verify_password(hashed, "s3cret-passphrase")
    assert not verify_password(hashed, "wrong")


def test_protected_page_redirects_to_login(client: TestClient) -> None:
    response = client.get("/app", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=/app"


def test_login_rejects_bad_password_and_missing_csrf(client: TestClient) -> None:
    make_tenant(client, slug="alpha")
    page = client.get("/login")
    csrf = CSRF_RE.search(page.text)
    assert csrf is not None

    bad = client.post(
        "/login",
        data={"email": "owner@alpha.example", "password": "wrong", "csrf": csrf.group(1)},
    )
    assert bad.status_code == 401
    assert "incorrect" in bad.text

    no_csrf = client.post(
        "/login", data={"email": "owner@alpha.example", "password": "CorrectHorse@2026"}
    )
    assert no_csrf.status_code == 403


def test_login_success_dashboard_and_logout(client: TestClient) -> None:
    org, _ = make_tenant(client, slug="alpha")
    make_order(client, org, "GEN-1001")
    login(client, "owner@alpha.example")

    dashboard = client.get("/app")
    assert dashboard.status_code == 200
    assert "alpha Retail" in dashboard.text
    assert "Returns control room" in dashboard.text

    orders = client.get("/app/orders")
    assert "GEN-1001" in orders.text

    csrf = CSRF_RE.search(dashboard.text)
    assert csrf is not None
    logout = client.post("/logout", data={"csrf": csrf.group(1)}, follow_redirects=False)
    assert logout.status_code == 303
    assert client.get("/app", follow_redirects=False).status_code == 303


def test_tenant_isolation_for_pages_and_api(client: TestClient) -> None:
    alpha, alpha_user = make_tenant(client, slug="alpha")
    beta, beta_user = make_tenant(client, slug="beta")
    alpha_order = make_order(client, alpha, "ALPHA-1")
    beta_order = make_order(client, beta, "BETA-1")
    alpha_key = make_api_key(client, alpha, alpha_user)
    beta_key = make_api_key(client, beta, beta_user)

    login(client, "owner@alpha.example")
    orders = client.get("/app/orders")
    assert "ALPHA-1" in orders.text
    assert "BETA-1" not in orders.text
    assert client.get(f"/app/orders/{beta_order.id}").status_code == 404
    assert client.get(f"/app/orders/{alpha_order.id}").status_code == 200

    alpha_headers = {"Authorization": f"Bearer {alpha_key}"}
    listing = client.get("/api/v1/orders", headers=alpha_headers)
    assert listing.status_code == 200
    numbers = {row["order_number"] for row in listing.json()}
    assert numbers == {"ALPHA-1"}
    assert client.get(f"/api/v1/orders/{beta_order.id}", headers=alpha_headers).status_code == 404

    beta_headers = {"X-API-Key": beta_key}
    beta_listing = client.get("/api/v1/orders", headers=beta_headers)
    assert {row["order_number"] for row in beta_listing.json()} == {"BETA-1"}
    assert client.get(f"/api/v1/orders/{alpha_order.id}", headers=beta_headers).status_code == 404


def test_api_key_auth_rejects_missing_invalid_and_revoked(client: TestClient) -> None:
    org, user = make_tenant(client, slug="alpha")
    key = make_api_key(client, org, user)

    assert client.get("/api/v1/orders").status_code == 401
    assert client.get("/api/v1/orders", headers={"Authorization": "Bearer rp_nope"}).status_code == 401

    me = client.get("/api/v1/me", headers={"Authorization": f"Bearer {key}"})
    assert me.status_code == 200
    assert me.json()["org_id"] == org.id
    assert me.json()["role"] == "owner"

    db = _session(client)
    try:
        db.query(ApiKey).filter_by(org_id=org.id).update({"revoked_at": utcnow()})
        db.commit()
    finally:
        db.close()
    assert client.get("/api/v1/orders", headers={"Authorization": f"Bearer {key}"}).status_code == 401


def test_api_key_creation_is_owner_only_and_token_is_not_stored(client: TestClient) -> None:
    org, owner = make_tenant(client, slug="alpha")
    viewer_org, viewer = make_tenant(client, slug="beta", role=Role.VIEWER)
    owner_key = make_api_key(client, org, owner)
    viewer_key = make_api_key(client, viewer_org, viewer)

    created = client.post(
        "/api/v1/api-keys",
        json={"name": "Warehouse scanner"},
        headers={"Authorization": f"Bearer {owner_key}"},
    )
    assert created.status_code == 201
    payload = created.json()
    assert payload["token"].startswith("rp_")
    assert payload["prefix"] == payload["token"][:12]

    db = _session(client)
    try:
        stored = db.query(ApiKey).filter_by(id=payload["id"]).one()
        assert stored.key_hash != payload["token"]
        assert payload["token"] not in stored.key_hash
    finally:
        db.close()

    denied = client.post(
        "/api/v1/api-keys",
        json={"name": "Should fail"},
        headers={"Authorization": f"Bearer {viewer_key}"},
    )
    assert denied.status_code == 403


def test_session_role_is_reflected_from_database(client: TestClient) -> None:
    _org, user = make_tenant(client, slug="alpha", role=Role.VIEWER)
    login(client, "viewer@alpha.example")
    page = client.get("/app")
    assert page.status_code == 200
    assert "Viewer" in page.text

    db = _session(client)
    try:
        db.query(User).filter_by(id=user.id).update({"role": Role.OWNER.value})
        db.commit()
    finally:
        db.close()
    # Session role no longer matches the database; the session is invalidated.
    assert client.get("/app", follow_redirects=False).status_code == 303
