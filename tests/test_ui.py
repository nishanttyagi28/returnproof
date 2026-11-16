from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from returnproof.domain.models import Claim

from tests.test_claims import create_claim, login, ready_return, session


def test_returns_list_filters_and_return_detail(client: TestClient) -> None:
    _org, user, _token, order, ret = ready_return(client)
    login(client, user.email)

    listing = client.get("/app/returns")
    assert listing.status_code == 200
    assert ret.rma_number in listing.text
    assert order.order_number in listing.text
    assert "Return requests" in listing.text

    mismatch = client.get("/app/returns?verification_filter=mismatch")
    assert ret.rma_number in mismatch.text
    pending = client.get("/app/returns?verification_filter=pending")
    assert ret.rma_number not in pending.text

    filtered = client.get(f"/app/returns?marketplace_filter={order.marketplace_id}")
    assert ret.rma_number in filtered.text
    missing = client.get("/app/returns?marketplace_filter=99999")
    assert ret.rma_number not in missing.text

    detail = client.get(f"/app/returns/{ret.id}")
    assert detail.status_code == 200
    assert "Verification" in detail.text
    assert "Serial number does not match" in detail.text
    assert "Evidence" in detail.text
    assert "dispatch.png" in detail.text
    assert "Timeline" in detail.text
    assert "Skip to content" in detail.text

    other_detail = client.get("/app/returns/99999")
    assert other_detail.status_code == 404


def test_dashboard_shows_claims_nearing_deadline(client: TestClient) -> None:
    _org, user, token, _order, ret = ready_return(client)
    claim = create_claim(client, token, ret.id, template_code="generic").json()
    filed = client.post(
        f"/api/v1/claims/{claim['id']}/file",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert filed.status_code == 200
    db = session(client)
    try:
        row = db.get(Claim, claim["id"])
        assert row is not None
        row.deadline_at = datetime.now(UTC) + timedelta(days=2)
        db.commit()
    finally:
        db.close()

    login(client, user.email)
    page = client.get("/app/dashboard") if False else client.get("/app")
    assert page.status_code == 200
    assert "Claims nearing deadline" in page.text
    assert claim["reference"] in page.text
    assert "Returns this week" in page.text
    assert "Mismatch rate" in page.text
    assert "Money at risk" in page.text


def test_orders_marketplace_filter_and_navigation(client: TestClient) -> None:
    _org, user, _token, order, _ret = ready_return(client)
    login(client, user.email)
    page = client.get(f"/app/orders?marketplace_filter={order.marketplace_id}")
    assert page.status_code == 200
    assert order.order_number in page.text
    assert "Marketplace" in page.text
    assert 'href="/app/returns"' in page.text
    assert 'href="/app/claims"' in page.text
    assert 'href="/app/recovery"' in page.text
