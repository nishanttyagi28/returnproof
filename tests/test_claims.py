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


def patterned_png() -> bytes:
    image = Image.new("RGB", (160, 120), "white")
    draw = ImageDraw.Draw(image)
    for index in range(8):
        x = (index * 21) % 140
        draw.rectangle([x, 4 + index * 14, x + 32, 24 + index * 14], fill=(25, 70, 135))
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
            password_hash=hash_password("Claims@2026"),
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
                name="claims key",
                prefix=prefix,
                key_hash=key_hash,
            )
        )
        db.commit()
        return org, user, token
    finally:
        db.close()


def order_with_item(
    client: TestClient, org: Organisation, number: str = "CLM-1", reason: str = "wrong_item"
) -> tuple[Order, ReturnRequest]:
    db = session(client)
    try:
        marketplace = db.query(Marketplace).filter_by(code="generic").one_or_none()
        if marketplace is None:
            marketplace = Marketplace(
                code="generic",
                name="Generic",
                claim_window_days=15,
                config={"template": "generic"},
            )
            db.add(marketplace)
            db.flush()
        order = Order(
            org_id=org.id,
            marketplace_id=marketplace.id,
            order_number=number,
            placed_at=datetime.now(UTC) - timedelta(days=12),
            dispatched_at=datetime.now(UTC) - timedelta(days=11),
            delivered_at=datetime.now(UTC) - timedelta(days=7),
            customer_name="Sample Buyer",
            customer_pincode="411001",
            customer_state="Maharashtra",
            status="returned",
            total_paise=249900,
            is_sample=True,
        )
        db.add(order)
        db.flush()
        db.add(
            OrderItem(
                order_id=order.id,
                sku_snapshot="CLM-SKU-1",
                title_snapshot="Claim sample item",
                quantity=1,
                unit_price_paise=249900,
                serial_number="SN-DISPATCH-CLM",
                imei="869000000000111",
                barcode="8901234567891",
                batch_code="BATCH-CLM",
                expected_weight_grams=210,
            )
        )
        ret = ReturnRequest(
            org_id=org.id,
            order_id=order.id,
            rma_number=f"RMA-{number}",
            requested_at=datetime.now(UTC) - timedelta(days=4),
            received_at=datetime.now(UTC) - timedelta(days=2),
            reason_code=reason,
            reason_text="Buyer says the item is different",
            status="received",
            refund_amount_paise=249900,
            is_sample=True,
        )
        db.add(ret)
        db.commit()
        db.refresh(order)
        db.refresh(ret)
        return order, ret
    finally:
        db.close()


def upload_photo(client: TestClient, token: str, order_id: int, stage: str, return_id: int | None) -> None:
    response = client.post(
        f"/api/v1/orders/{order_id}/evidence",
        headers={"Authorization": f"Bearer {token}"},
        data={"stage": stage, "kind": "photo", "return_request_id": str(return_id or "")},
        files={"file": (f"{stage}.png", patterned_png(), "image/png")},
    )
    assert response.status_code == 201, response.text


def verify_mismatch(client: TestClient, token: str, return_id: int) -> None:
    response = client.post(
        f"/api/v1/returns/{return_id}/verification",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "serial_number": "SN-OTHER",
            "imei": "869000000000111",
            "barcode": "8901234567891",
            "batch_code": "BATCH-CLM",
            "seal_intact": True,
            "accessories_present": True,
            "tags_present": True,
            "weight_grams": 210,
        },
    )
    assert response.status_code == 200
    assert response.json()["status"] == "mismatch"


def ready_return(
    client: TestClient, slug: str = "alpha"
) -> tuple[Organisation, User, str, Order, ReturnRequest]:
    org, user, token = tenant(client, slug)
    order, ret = order_with_item(client, org)
    upload_photo(client, token, order.id, "dispatch", None)
    upload_photo(client, token, order.id, "return_receipt", ret.id)
    verify_mismatch(client, token, ret.id)
    return org, user, token, order, ret


def create_claim(client: TestClient, token: str, return_id: int, **body: object) -> object:
    return client.post(
        f"/api/v1/returns/{return_id}/claims",
        headers={"Authorization": f"Bearer {token}"},
        json=body,
    )


def login(client: TestClient, email: str) -> None:
    page = client.get("/login")
    csrf = CSRF_RE.search(page.text)
    assert csrf is not None
    response = client.post(
        "/login",
        data={"email": email, "password": "Claims@2026", "csrf": csrf.group(1)},
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_claim_pack_html_and_pdf_contain_evidence(client: TestClient) -> None:
    _org, _user, token, order, ret = ready_return(client)
    response = create_claim(client, token, ret.id, template_code="generic")
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "draft"
    assert body["amount_claimed_paise"] == 249900
    assert body["pack_path"]

    claim_id = body["id"]
    html = client.get(
        f"/api/v1/claims/{claim_id}/pack?format=html",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert html.status_code == 200
    assert order.order_number in html.text
    assert "Claim summary" in html.text
    assert "Serial number does not match" in html.text
    assert "data:image/jpeg;base64," in html.text
    assert "SHA-256" in html.text

    pdf = client.get(
        f"/api/v1/claims/{claim_id}/pack?format=pdf",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert pdf.status_code == 200
    assert pdf.headers["content-type"] == "application/pdf"
    assert pdf.content.startswith(b"%PDF")
    assert len(pdf.content) > 3000

    pack_path = client.app.state.settings.pack_dir / body["pack_path"]
    assert pack_path.is_file()
    assert pack_path.with_suffix(".pdf").is_file()


def test_marketplace_templates_are_data_driven(client: TestClient) -> None:
    templates = client.get("/api/v1/claim-templates").json()
    codes = {item["code"] for item in templates}
    assert {"generic", "amazon-safe-t", "flipkart"} <= codes
    amazon = next(item for item in templates if item["code"] == "amazon-safe-t")
    generic = next(item for item in templates if item["code"] == "generic")
    assert amazon["sections"] != generic["sections"]
    assert "SAFE-T" in amazon["name"]

    _org, _user, token, _order, ret = ready_return(client)
    amazon_claim = create_claim(client, token, ret.id, template_code="amazon-safe-t")
    assert amazon_claim.status_code == 201
    html = client.get(
        f"/api/v1/claims/{amazon_claim.json()['id']}/pack?format=html",
        headers={"Authorization": f"Bearer {token}"},
    ).text
    assert "SAFE-T reimbursement claim pack" in html
    assert amazon["name"] in html

    unknown = create_claim(client, token, ret.id, template_code="does-not-exist")
    assert unknown.status_code == 409
    assert "Unknown claim template" in unknown.json()["detail"]


def test_claim_eligibility_requires_mismatch_or_damage(client: TestClient) -> None:
    org, _user, token = tenant(client, "alpha")
    _order, ret = order_with_item(client, org, "CLM-INELIGIBLE", reason="size_fit")
    ineligible = create_claim(client, token, ret.id)
    assert ineligible.status_code == 409
    assert "MISMATCH" in ineligible.json()["detail"]

    db = session(client)
    try:
        row = db.query(ReturnRequest).filter_by(id=ret.id).one()
        row.reason_code = "damaged"
        db.commit()
    finally:
        db.close()
    allowed = create_claim(client, token, ret.id)
    assert allowed.status_code == 201
