from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from io import BytesIO

from fastapi.testclient import TestClient
from PIL import Image, ImageDraw
from returnproof.auth import generate_api_key, hash_password
from returnproof.domain.enums import Role
from returnproof.domain.models import (
    ApiKey,
    Marketplace,
    Order,
    OrderItem,
    Organisation,
    ReturnRequest,
    User,
)
from sqlalchemy.orm import Session

CSRF_RE = re.compile(r'name="csrf" value="([^"]+)"')


def patterned_png(shift: int = 0, invert: bool = False) -> bytes:
    image = Image.new("RGB", (160, 120), "white")
    draw = ImageDraw.Draw(image)
    for index in range(8):
        x = (index * 23 + shift) % 150
        color = (20, 60, 120) if not invert else (230, 200, 150)
        draw.rectangle([x, 5 + index * 14, x + 35, 25 + index * 14], fill=color)
        draw.line([0, 10 + index * 14, 159, 30 + index * 14], fill="black", width=1)
    buffer = BytesIO()
    image.save(buffer, format="PNG")
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
            password_hash=hash_password("Verify@2026"),
            full_name=f"{slug.title()} {role.value.title()}",
            role=role.value,
        )
        db.add(user)
        db.flush()
        token, prefix, key_hash = generate_api_key()
        db.add(
            ApiKey(
                org_id=org.id,
                created_by_id=user.id,
                name="verification key",
                prefix=prefix,
                key_hash=key_hash,
            )
        )
        db.commit()
        return org, user, token
    finally:
        db.close()


def order_with_item(client: TestClient, org: Organisation, number: str = "VER-1") -> tuple[Order, OrderItem]:
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
            placed_at=datetime.now(UTC) - timedelta(days=8),
            customer_name="Sample Buyer",
            customer_pincode="411001",
            customer_state="Maharashtra",
            status="returned",
            total_paise=249900,
            is_sample=True,
        )
        db.add(order)
        db.flush()
        item = OrderItem(
            order_id=order.id,
            sku_snapshot="VER-SKU-1",
            title_snapshot="Verification sample item",
            quantity=1,
            unit_price_paise=249900,
            serial_number="SN-DISPATCH-001",
            imei="869000000000001",
            barcode="8901234567890",
            batch_code="BATCH-NOV-1",
            expected_weight_grams=210,
        )
        db.add(item)
        db.commit()
        db.refresh(order)
        db.refresh(item)
        return order, item
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


def upload_photo(
    client: TestClient,
    token: str,
    order_id: int,
    *,
    stage: str,
    content: bytes,
    return_id: int | None = None,
    name: str = "photo.png",
) -> None:
    response = client.post(
        f"/api/v1/orders/{order_id}/evidence",
        headers={"Authorization": f"Bearer {token}"},
        data={
            "stage": stage,
            "kind": "photo",
            "device_note": "verification fixture",
            "return_request_id": str(return_id or ""),
        },
        files={"file": (name, content, "image/png")},
    )
    assert response.status_code == 201, response.text


def verify(
    client: TestClient,
    token: str,
    return_id: int,
    *,
    serial: str | None = "SN-DISPATCH-001",
    imei: str | None = "869000000000001",
    barcode: str | None = "8901234567890",
    batch: str | None = "BATCH-NOV-1",
    seal: bool | None = True,
    accessories: bool | None = True,
    tags: bool | None = True,
    weight: int | None = 210,
) -> object:
    return client.post(
        f"/api/v1/returns/{return_id}/verification",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "serial_number": serial,
            "imei": imei,
            "barcode": barcode,
            "batch_code": batch,
            "seal_intact": seal,
            "accessories_present": accessories,
            "tags_present": tags,
            "weight_grams": weight,
        },
    )


def login(client: TestClient, email: str) -> None:
    page = client.get("/login")
    csrf = CSRF_RE.search(page.text)
    assert csrf is not None
    response = client.post(
        "/login",
        data={"email": email, "password": "Verify@2026", "csrf": csrf.group(1)},
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_full_match_with_identical_photo_and_identifiers(client: TestClient) -> None:
    org, _user, token = tenant(client, "alpha")
    order, _item = order_with_item(client, org)
    ret = return_for(client, order)
    photo = patterned_png()
    upload_photo(client, token, order.id, stage="dispatch", content=photo)
    upload_photo(
        client, token, order.id, stage="return_receipt", content=photo, return_id=ret.id, name="ret.png"
    )

    response = verify(client, token, ret.id)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "match"
    assert body["score"] > 0.9
    assert body["image_distance"] == 0
    assert body["identifier_checks"]["serial_number"]["status"] == "match"
    assert body["checklist"]["weight_ok"] is True
    assert body["returned_identifiers"]["serial_number"] == "SN-DISPATCH-001"


def test_identifier_mismatch_is_a_hard_mismatch(client: TestClient) -> None:
    org, _user, token = tenant(client, "alpha")
    order, _item = order_with_item(client, org)
    ret = return_for(client, order)
    photo = patterned_png()
    upload_photo(client, token, order.id, stage="dispatch", content=photo)
    upload_photo(client, token, order.id, stage="return_receipt", content=photo, return_id=ret.id)

    body = verify(client, token, ret.id, serial="SN-OTHER-999").json()
    assert body["status"] == "mismatch"
    assert any("Serial number does not match" in reason for reason in body["reasons"])


def test_visual_difference_is_a_mismatch(client: TestClient) -> None:
    org, _user, token = tenant(client, "alpha")
    order, _item = order_with_item(client, org)
    ret = return_for(client, order)
    upload_photo(client, token, order.id, stage="dispatch", content=patterned_png())
    upload_photo(
        client,
        token,
        order.id,
        stage="return_receipt",
        content=patterned_png(shift=17, invert=True),
        return_id=ret.id,
    )

    body = verify(client, token, ret.id).json()
    assert body["image_distance"] is not None
    assert body["image_distance"] > 8
    assert body["status"] == "mismatch"
    assert any("Visual hash distance" in reason for reason in body["reasons"])


def test_missing_return_photo_needs_review(client: TestClient) -> None:
    org, _user, token = tenant(client, "alpha")
    order, _item = order_with_item(client, org)
    ret = return_for(client, order)
    upload_photo(client, token, order.id, stage="dispatch", content=patterned_png())

    body = verify(client, token, ret.id).json()
    assert body["status"] == "needs_review"
    assert any("return-receipt photograph" in reason for reason in body["reasons"])


def test_missing_typed_identifiers_needs_review(client: TestClient) -> None:
    org, _user, token = tenant(client, "alpha")
    order, _item = order_with_item(client, org)
    ret = return_for(client, order)
    photo = patterned_png()
    upload_photo(client, token, order.id, stage="dispatch", content=photo)
    upload_photo(client, token, order.id, stage="return_receipt", content=photo, return_id=ret.id)

    body = verify(
        client,
        token,
        ret.id,
        serial=None,
        imei=None,
        barcode=None,
        batch=None,
    ).json()
    assert body["status"] == "needs_review"
    assert any("not captured at return" in reason for reason in body["reasons"])


def test_checklist_failures_and_weight_tolerance(client: TestClient) -> None:
    org, _user, token = tenant(client, "alpha")
    order, _item = order_with_item(client, org)
    ret = return_for(client, order)
    photo = patterned_png()
    upload_photo(client, token, order.id, stage="dispatch", content=photo)
    upload_photo(client, token, order.id, stage="return_receipt", content=photo, return_id=ret.id)

    broken_seal = verify(client, token, ret.id, seal=False).json()
    assert broken_seal["status"] == "mismatch"
    assert any("Seal check failed" in reason for reason in broken_seal["reasons"])

    heavy = verify(client, token, ret.id, weight=400).json()
    assert heavy["status"] == "mismatch"
    assert heavy["checklist"]["weight_ok"] is False
    assert any("outside 210 g" in reason for reason in heavy["reasons"])

    within_tolerance = verify(client, token, ret.id, weight=225).json()
    assert within_tolerance["status"] == "match"
    assert within_tolerance["checklist"]["weight_variance_grams"] == 15


def test_csv_import_reports_results_and_unknown_rmas(client: TestClient) -> None:
    org, _user, token = tenant(client, "alpha")
    order, _item = order_with_item(client, org)
    ret = return_for(client, order)
    photo = patterned_png()
    upload_photo(client, token, order.id, stage="dispatch", content=photo)
    upload_photo(client, token, order.id, stage="return_receipt", content=photo, return_id=ret.id)

    csv_text = (
        "rma_number,order_number,serial_number,imei,barcode,batch_code,weight_grams,"
        "seal_intact,accessories_present,tags_present\n"
        f"{ret.rma_number},{order.order_number},SN-DISPATCH-001,869000000000001,"
        "8901234567890,BATCH-NOV-1,208,yes,yes,yes\n"
        "RMA-UNKNOWN,,SN-X,,,,200,yes,yes,yes\n"
    )
    response = client.post(
        "/api/v1/verifications/import",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("identifiers.csv", csv_text.encode(), "text/csv")},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["processed"] == 1
    assert body["results"][0]["status"] == "match"
    assert body["errors"][0]["row_number"] == 3
    assert "No return found" in body["errors"][0]["message"]


def test_verification_is_tenant_scoped_and_role_guarded(client: TestClient) -> None:
    alpha, _u, alpha_token = tenant(client, "alpha")
    _beta, _b, beta_token = tenant(client, "beta")
    order, _item = order_with_item(client, alpha)
    ret = return_for(client, order)
    viewer_org, _vu, viewer_token = tenant(client, "gamma", role=Role.VIEWER)
    _viewer_order, _vi = order_with_item(client, viewer_org, "VER-VIEW")
    viewer_ret = return_for(client, _viewer_order)

    assert verify(client, beta_token, ret.id).status_code == 404
    assert verify(client, viewer_token, viewer_ret.id).status_code == 403
    assert verify(client, alpha_token, ret.id).status_code == 200


def test_ui_verification_updates_order_detail(client: TestClient) -> None:
    org, user, token = tenant(client, "alpha")
    order, _item = order_with_item(client, org)
    ret = return_for(client, order)
    photo = patterned_png()
    upload_photo(client, token, order.id, stage="dispatch", content=photo)
    upload_photo(client, token, order.id, stage="return_receipt", content=photo, return_id=ret.id)
    login(client, user.email)

    detail = client.get(f"/app/orders/{order.id}")
    csrf = CSRF_RE.search(detail.text)
    assert csrf is not None
    response = client.post(
        f"/app/returns/{ret.id}/verify",
        data={
            "csrf": csrf.group(1),
            "serial_number": "SN-DISPATCH-001",
            "imei": "869000000000001",
            "barcode": "8901234567890",
            "batch_code": "BATCH-NOV-1",
            "weight_grams": "210",
            "seal_intact": "yes",
            "accessories_present": "yes",
            "tags_present": "yes",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    page = client.get(f"/app/orders/{order.id}")
    assert "Verification complete: Match" in page.text
    assert "score 1.00" in page.text
