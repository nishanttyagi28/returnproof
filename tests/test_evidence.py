from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from io import BytesIO

from fastapi.testclient import TestClient
from PIL import Image
from returnproof.auth import generate_api_key, hash_password
from returnproof.domain.enums import Role
from returnproof.domain.models import (
    ApiKey,
    Evidence,
    Marketplace,
    Order,
    Organisation,
    ReturnRequest,
    User,
)
from sqlalchemy.orm import Session

CSRF_RE = re.compile(r'name="csrf" value="([^"]+)"')


def png_bytes(color: str = "teal", size: tuple[int, int] = (64, 64)) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", size, color=color).save(buffer, format="PNG")
    return buffer.getvalue()


def session(client: TestClient) -> Session:
    return client.app.state.session_factory()


def tenant(client: TestClient, slug: str, *, role: Role = Role.OWNER) -> tuple[Organisation, User, str]:
    db = session(client)
    try:
        org = Organisation(
            name=f"{slug} Retail",
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
            password_hash=hash_password("Evidence@2026"),
            full_name=f"{slug.title()} {role.value.title()}",
            role=role.value,
        )
        db.add(user)
        db.commit()
        token, prefix, key_hash = generate_api_key()
        db.add(
            ApiKey(
                org_id=org.id,
                created_by_id=user.id,
                name="evidence key",
                prefix=prefix,
                key_hash=key_hash,
            )
        )
        db.commit()
        return org, user, token
    finally:
        db.close()


def order_for(client: TestClient, org: Organisation, number: str = "EVID-1") -> Order:
    db = session(client)
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
            placed_at=datetime.now(UTC) - timedelta(days=5),
            dispatched_at=datetime.now(UTC) - timedelta(days=4),
            customer_name="Sample Buyer",
            customer_pincode="411001",
            customer_state="Maharashtra",
            status="delivered",
            total_paise=249900,
            is_sample=True,
        )
        db.add(order)
        db.commit()
        db.refresh(order)
        return order
    finally:
        db.close()


def return_for(client: TestClient, order: Order) -> ReturnRequest:
    db = session(client)
    try:
        record = ReturnRequest(
            org_id=order.org_id,
            order_id=order.id,
            rma_number=f"RMA-{order.order_number}",
            requested_at=datetime.now(UTC) - timedelta(days=2),
            status="received",
            refund_amount_paise=order.total_paise,
            is_sample=True,
        )
        db.add(record)
        db.commit()
        db.refresh(record)
        return record
    finally:
        db.close()


def api_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def upload(
    client: TestClient,
    token: str,
    order_id: int,
    *,
    stage: str = "dispatch",
    kind: str = "photo",
    filename: str = "dispatch.png",
    content: bytes | None = None,
    content_type: str = "image/png",
    return_request_id: int | None = None,
    device_note: str = "Sample camera",
) -> object:
    data = {
        "stage": stage,
        "kind": kind,
        "device_note": device_note,
        "return_request_id": str(return_request_id or ""),
    }
    return client.post(
        f"/api/v1/orders/{order_id}/evidence",
        headers=api_headers(token),
        data=data,
        files={"file": (filename, content if content is not None else png_bytes(), content_type)},
    )


def login(client: TestClient, email: str) -> None:
    page = client.get("/login")
    csrf = CSRF_RE.search(page.text)
    assert csrf is not None
    response = client.post(
        "/login",
        data={"email": email, "password": "Evidence@2026", "csrf": csrf.group(1)},
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_upload_builds_append_only_chain_and_validates(client: TestClient) -> None:
    org, _user, token = tenant(client, "alpha")
    order = order_for(client, org)
    ret = return_for(client, order)

    first = upload(client, token, order.id, stage="dispatch")
    assert first.status_code == 201, first.text
    first_body = first.json()
    assert first_body["chain_index"] == 0
    assert first_body["previous_hash"] == "0" * 64
    assert len(first_body["sha256"]) == 64
    assert first_body["width"] == 64
    assert first_body["perceptual_hash"]

    second = upload(
        client,
        token,
        order.id,
        stage="return_receipt",
        filename="receipt.png",
        content=png_bytes("orange"),
        return_request_id=ret.id,
    )
    assert second.status_code == 201, second.text
    second_body = second.json()
    assert second_body["chain_index"] == 1
    assert second_body["previous_hash"] == first_body["chain_hash"]

    report = client.get(f"/api/v1/orders/{order.id}/evidence/check", headers=api_headers(token))
    assert report.status_code == 200
    body = report.json()
    assert body["valid"] is True
    assert body["evidence_count"] == 2
    assert body["head_hash"] == second_body["chain_hash"]
    assert body["problems"] == []

    listing = client.get(f"/api/v1/orders/{order.id}/evidence", headers=api_headers(token))
    assert [row["filename"] for row in listing.json()] == ["dispatch.png", "receipt.png"]


def test_file_tampering_is_detected(client: TestClient) -> None:
    org, _user, token = tenant(client, "alpha")
    order = order_for(client, org)
    assert upload(client, token, order.id).status_code == 201

    db = session(client)
    try:
        evidence = db.query(Evidence).filter_by(order_id=order.id).one()
        path = client.app.state.settings.upload_dir / evidence.stored_path
    finally:
        db.close()
    path.write_bytes(b"tampered bytes")

    report = client.get(f"/api/v1/orders/{order.id}/evidence/check", headers=api_headers(token)).json()
    assert report["valid"] is False
    codes = {problem["code"] for problem in report["problems"]}
    assert "FILE_HASH_MISMATCH" in codes


def test_metadata_tampering_is_detected(client: TestClient) -> None:
    org, _user, token = tenant(client, "alpha")
    order = order_for(client, org)
    assert upload(client, token, order.id).status_code == 201

    db = session(client)
    try:
        evidence = db.query(Evidence).filter_by(order_id=order.id).one()
        evidence.device_note = "edited after the fact"
        db.commit()
    finally:
        db.close()

    report = client.get(f"/api/v1/orders/{order.id}/evidence/check", headers=api_headers(token)).json()
    assert report["valid"] is False
    assert "CHAIN_HASH_MISMATCH" in {problem["code"] for problem in report["problems"]}


def test_invalid_uploads_are_rejected(client: TestClient) -> None:
    org, _user, token = tenant(client, "alpha")
    order = order_for(client, org)

    bad_type = upload(
        client, token, order.id, kind="photo", content=b"not an image", content_type="text/plain"
    )
    assert bad_type.status_code == 422
    assert "not allowed" in bad_type.json()["detail"]

    corrupt_image = upload(client, token, order.id, kind="photo", content=b"not an image")
    assert corrupt_image.status_code == 422

    no_return = upload(client, token, order.id, stage="return_receipt")
    assert no_return.status_code == 422
    assert "return request" in no_return.json()["detail"]

    empty = upload(client, token, order.id, content=b"")
    assert empty.status_code == 422


def test_viewer_keys_cannot_upload_and_cross_tenant_is_hidden(client: TestClient) -> None:
    alpha, _alpha_user, alpha_token = tenant(client, "alpha")
    beta, _beta_user, beta_token = tenant(client, "beta")
    alpha_order = order_for(client, alpha, "ALPHA-EV")
    beta_order = order_for(client, beta, "BETA-EV")

    by_id = tenant(client, "gamma", role=Role.VIEWER)
    viewer_token = by_id[2]
    viewer_order = order_for(client, by_id[0], "GAMMA-EV")
    denied = upload(client, viewer_token, viewer_order.id)
    assert denied.status_code == 403

    hidden = upload(client, alpha_token, beta_order.id)
    assert hidden.status_code == 404
    assert (
        client.get(
            f"/api/v1/orders/{beta_order.id}/evidence/check", headers=api_headers(alpha_token)
        ).status_code
        == 404
    )
    assert (
        client.get(
            f"/api/v1/orders/{alpha_order.id}/evidence/check", headers=api_headers(beta_token)
        ).status_code
        == 404
    )


def test_ui_upload_and_gallery_with_csrf(client: TestClient) -> None:
    org, user, _token = tenant(client, "alpha")
    order = order_for(client, org)
    login(client, user.email)

    detail = client.get(f"/app/orders/{order.id}")
    assert detail.status_code == 200
    csrf = CSRF_RE.search(detail.text)
    assert csrf is not None

    response = client.post(
        f"/app/orders/{order.id}/evidence",
        data={
            "csrf": csrf.group(1),
            "stage": "dispatch",
            "kind": "photo",
            "device_note": "Packing bench 1",
            "return_request_id": "",
        },
        files={"file": ("packing.png", png_bytes("navy"), "image/png")},
        follow_redirects=False,
    )
    assert response.status_code == 303

    page = client.get(f"/app/orders/{order.id}")
    assert "Evidence added to the order hash chain" in page.text
    assert "packing.png" in page.text
    assert "Chain verified" in page.text

    db = session(client)
    try:
        evidence = db.query(Evidence).filter_by(order_id=order.id).one()
        evidence_id = evidence.id
    finally:
        db.close()
    served = client.get(f"/app/evidence/{evidence_id}/file")
    assert served.status_code == 200
    assert served.headers["content-type"].startswith("image/png")
    assert served.content[:8] == b"\x89PNG\r\n\x1a\n"

    check_page = client.get(f"/app/orders/{order.id}/evidence/check")
    assert check_page.status_code == 200
    assert "Evidence tamper check" in check_page.text

    other_org, _other_user, _other_token = tenant(client, "beta")
    other_order = order_for(client, other_org, "BETA-UI")
    assert client.get(f"/app/orders/{other_order.id}").status_code == 404


def test_chain_indexes_are_per_order_and_isolated(client: TestClient) -> None:
    org, _user, token = tenant(client, "alpha")
    first_order = order_for(client, org, "CHAIN-A")
    second_order = order_for(client, org, "CHAIN-B")

    a1 = upload(client, token, first_order.id).json()
    a2 = upload(client, token, first_order.id, content=png_bytes("red")).json()
    b1 = upload(client, token, second_order.id, content=png_bytes("blue")).json()

    assert (a1["chain_index"], a2["chain_index"], b1["chain_index"]) == (0, 1, 0)
    assert b1["previous_hash"] == "0" * 64
    assert a2["previous_hash"] == a1["chain_hash"]
