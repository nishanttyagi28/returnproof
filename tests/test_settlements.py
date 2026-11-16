from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from returnproof.domain.enums import Role
from returnproof.domain.models import Claim, Marketplace, RecoveryEntry
from returnproof.settlements import SettlementError, parse_money, parse_settlement_csv
from sqlalchemy.orm import Session

from tests.test_claims import (  # type: ignore[import-not-found]
    CSRF_RE,
    login,
    order_with_item,
    session,
    tenant,
)

HEADER = (
    "order_number,transaction_type,marketplace_sku,quantity,gross_inr,commission_inr,"
    "shipping_inr,reverse_shipping_inr,refund_inr,other_fee_inr,net_inr,claim_payout_inr"
)


def money(paise: int) -> str:
    return f"{paise / 100:.2f}"


def csv_rows(rows: list[dict[str, int | str]]) -> bytes:
    lines = [HEADER]
    for row in rows:
        gross = int(row.get("gross", 0))
        commission = int(row.get("commission", 0))
        shipping = int(row.get("shipping", 0))
        reverse = int(row.get("reverse", 0))
        refund = int(row.get("refund", 0))
        other = int(row.get("other", 0))
        net = int(row.get("net", gross - commission - shipping - reverse - refund - other))
        lines.append(
            ",".join(
                [
                    str(row["order"]),
                    str(row.get("type", "sale")),
                    str(row.get("sku", "SAMPLE-SKU")),
                    str(row.get("quantity", 1)),
                    money(gross),
                    money(commission),
                    money(shipping),
                    money(reverse),
                    money(refund),
                    money(other),
                    money(net),
                    money(int(row.get("claim_payout", 0))),
                ]
            )
        )
    return ("\n".join(lines) + "\n").encode()


def ensure_marketplace(db: Session) -> Marketplace:
    marketplace = db.query(Marketplace).filter_by(code="generic").one_or_none()
    if marketplace is None:
        marketplace = Marketplace(
            code="generic",
            name="Generic",
            claim_window_days=15,
            config={"template": "generic", "commission_percent": 12},
        )
        db.add(marketplace)
        db.flush()
    return marketplace


def marketplace_id(client: TestClient) -> int:
    db = session(client)
    try:
        marketplace = ensure_marketplace(db)
        db.commit()
        return marketplace.id
    finally:
        db.close()


def import_csv(
    client: TestClient,
    token: str,
    content: bytes,
    *,
    period_start: str = "2026-11-01",
    period_end: str = "2026-11-15",
) -> object:
    return client.post(
        "/api/v1/settlements/import",
        headers={"Authorization": f"Bearer {token}"},
        data={
            "marketplace_id": str(marketplace_id(client)),
            "period_start": period_start,
            "period_end": period_end,
        },
        files={"file": ("settlement.csv", content, "text/csv")},
    )


def bare_order(client: TestClient, org_id: int, number: str) -> int:
    db = session(client)
    try:
        marketplace = ensure_marketplace(db)
        from returnproof.domain.models import Order

        order = Order(
            org_id=org_id,
            marketplace_id=marketplace.id,
            order_number=number,
            placed_at=datetime.now(UTC) - timedelta(days=20),
            customer_name="Sample Buyer",
            customer_pincode="411001",
            customer_state="Maharashtra",
            status="delivered",
            total_paise=100000,
            is_sample=True,
        )
        db.add(order)
        db.commit()
        db.refresh(order)
        return order.id
    finally:
        db.close()


def approved_claim(client: TestClient, org_id: int, order_id: int, return_id: int, amount: int) -> int:
    db = session(client)
    try:
        marketplace = ensure_marketplace(db)
        claim = Claim(
            org_id=org_id,
            order_id=order_id,
            return_request_id=return_id,
            marketplace_id=marketplace.id,
            reference=f"CLM-TEST-{amount}",
            status="approved",
            template_code="generic",
            amount_claimed_paise=amount,
            amount_approved_paise=amount,
            filed_at=datetime.now(UTC) - timedelta(days=3),
            deadline_at=datetime.now(UTC) + timedelta(days=5),
            resolved_at=datetime.now(UTC) - timedelta(days=1),
            is_sample=True,
        )
        db.add(claim)
        db.commit()
        db.refresh(claim)
        return claim.id
    finally:
        db.close()


def test_money_and_csv_aliases_parse() -> None:
    assert parse_money("₹1,234.56") == 123456
    assert parse_money("(1,234.50)") == -123450
    assert parse_money("INR 99") == 9900
    assert parse_money("") == 0
    with pytest.raises(SettlementError):
        parse_money("not-money")

    aliased = (
        "order-id,transaction-type,product-sales,commission,shipping,net-amount\n"
        "AMZ-1,order_payment,2499.00,299.88,72.00,2127.12\n"
    )
    rows, issues = parse_settlement_csv(aliased)
    assert issues == []
    assert rows[0].order_number == "AMZ-1"
    assert rows[0].transaction_type == "sale"
    assert rows[0].gross_paise == 249900
    assert rows[0].net_paise == 212712


def test_import_is_idempotent_and_scoped(client: TestClient) -> None:
    org, _user, token = tenant(client, "alpha")
    bare_order(client, org.id, "SET-1")
    content = csv_rows(
        [{"order": "SET-1", "gross": 249900, "commission": 29988, "shipping": 7200}]
    )
    response = import_csv(client, token, content)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["rows"] == 1
    assert body["settlement"]["total_net_paise"] == 212712
    assert body["entries_created"] == 0

    duplicate = import_csv(client, token, content)
    assert duplicate.status_code == 409

    listing = client.get("/api/v1/settlements", headers={"Authorization": f"Bearer {token}"})
    assert len(listing.json()) == 1
    detail = client.get(
        f"/api/v1/settlements/{body['settlement']['id']}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert detail.status_code == 200
    assert detail.json()["lines"][0]["order_number"] == "SET-1"
    assert detail.json()["lines"][0]["gross_paise"] == 249900

    _beta, _bu, beta_token = tenant(client, "beta")
    beta_headers = {"Authorization": f"Bearer {beta_token}"}
    assert client.get(
        f"/api/v1/settlements/{body['settlement']['id']}", headers=beta_headers
    ).status_code == 404
    assert client.get("/api/v1/settlements", headers=beta_headers).json() == []


def test_leakage_detection_refund_reverse_commission_and_fees(client: TestClient) -> None:
    org, _user, token = tenant(client, "alpha")
    bare_order(client, org.id, "NO-RETURN")
    _order_damaged, damaged_ret = order_with_item(client, org, "DAMAGED-1", reason="damaged")
    bare_order(client, org.id, "COMM-1")
    bare_order(client, org.id, "FEE-1")
    content = csv_rows(
        [
            {"order": "NO-RETURN", "type": "refund", "refund": 100000, "net": -100000},
            {
                "order": "DAMAGED-1",
                "gross": 100000,
                "commission": 12000,
                "reverse": 5000,
            },
            {"order": "COMM-1", "gross": 100000, "commission": 20000},
            {"order": "FEE-1", "gross": 100000, "commission": 12000, "net": 70000},
        ]
    )
    response = import_csv(client, token, content)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["entries_created"] == 4
    assert body["detected_paise"] == 100000 + 5000 + 8000 + 18000

    db = session(client)
    try:
        entries = db.query(RecoveryEntry).filter_by(org_id=org.id).all()
        categories = {entry.category for entry in entries}
        assert "refund_without_return" in categories
        assert "reverse_shipping_error" in categories
        assert "commission_mismatch" in categories
        assert "fee_mismatch" in categories
        assert all(entry.status == "open" for entry in entries)
        assert damaged_ret.reason_code == "damaged"
    finally:
        db.close()

    settlement_id = body["settlement"]["id"]
    rerun = client.post(
        f"/api/v1/settlements/{settlement_id}/reconcile",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert rerun.status_code == 200
    assert rerun.json()["entries_created"] == 0

    summary = client.get(
        "/api/v1/recovery/summary", headers={"Authorization": f"Bearer {token}"}
    ).json()
    assert summary["total_at_risk_paise"] == 131000
    assert summary["total_recovered_paise"] == 0
    assert summary["rows"][0]["marketplace"] == "Generic"

    entries = client.get(
        "/api/v1/recovery/entries?status=open",
        headers={"Authorization": f"Bearer {token}"},
    ).json()
    assert len(entries) == 4
    entry_id = entries[0]["id"]
    recovered = client.post(
        f"/api/v1/recovery/entries/{entry_id}/recover",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert recovered.status_code == 200
    assert recovered.json()["status"] == "recovered"
    summary_after = client.get(
        "/api/v1/recovery/summary", headers={"Authorization": f"Bearer {token}"}
    ).json()
    assert summary_after["total_recovered_paise"] == recovered.json()["amount_paise"]
    assert summary_after["total_at_risk_paise"] < summary["total_at_risk_paise"]


def test_approved_claim_without_payout_and_payout_recovery(client: TestClient) -> None:
    org, _user, token = tenant(client, "alpha")
    order, ret = order_with_item(client, org, "CLAIM-PAY", reason="wrong_item")
    claim_id = approved_claim(client, org.id, order.id, ret.id, 40000)

    first = client.post(
        "/api/v1/recovery/reconcile",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert first.status_code == 200, first.text
    assert first.json()["entries_created"] == 1
    assert first.json()["by_category"]["approved_not_paid"] == 40000

    db = session(client)
    try:
        entry = db.query(RecoveryEntry).filter_by(claim_id=claim_id).one()
        assert entry.status == "open"
        assert entry.direction == "at_risk"
    finally:
        db.close()

    content = csv_rows(
        [
            {
                "order": "CLAIM-PAY",
                "type": "claim_payout",
                "gross": 40000,
                "claim_payout": 40000,
                "net": 40000,
            }
        ]
    )
    imported = import_csv(client, token, content)
    assert imported.status_code == 201, imported.text
    assert imported.json()["entries_recovered"] == 1

    db = session(client)
    try:
        entry = db.query(RecoveryEntry).filter_by(claim_id=claim_id).one()
        assert entry.status == "recovered"
        assert entry.direction == "recovered"
        assert entry.recovered_at is not None
    finally:
        db.close()


def test_settlement_validation_and_roles(client: TestClient) -> None:
    _org, _user, token = tenant(client, "alpha")
    bad = import_csv(client, token, b"nonsense,data\n1,2\n")
    assert bad.status_code == 422
    missing = import_csv(client, token, b"order_number,transaction_type\nSET-9,sale\n")
    assert missing.status_code == 422

    _viewer_org, _viewer, viewer_token = tenant(client, "gamma", role=Role.VIEWER)
    denied = import_csv(
        client,
        viewer_token,
        csv_rows([{"order": "SET-9", "gross": 1000, "commission": 120}]),
    )
    assert denied.status_code == 403


def test_ui_settlement_import_and_recovery_report(client: TestClient) -> None:
    org, user, _token = tenant(client, "alpha")
    bare_order(client, org.id, "UI-SET-1")
    login(client, user.email)
    page = client.get("/app/settlements")
    assert page.status_code == 200
    csrf = CSRF_RE.search(page.text)
    assert csrf is not None
    response = client.post(
        "/app/settlements/import",
        data={
            "csrf": csrf.group(1),
            "marketplace_id": str(marketplace_id(client)),
            "period_start": "2026-11-01",
            "period_end": "2026-11-15",
        },
        files={
            "file": (
                "ui.csv",
                csv_rows([{"order": "UI-SET-1", "type": "refund", "refund": 50000, "net": -50000}]),
                "text/csv",
            )
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    after = client.get("/app/settlements")
    assert "Imported 1 settlement rows" in after.text
    assert "ui.csv" in after.text

    recovery = client.get("/app/recovery")
    assert recovery.status_code == 200
    assert "Money at risk vs recovered" in recovery.text
    assert "Refund Without Return" in recovery.text
    assert "₹500" in recovery.text
