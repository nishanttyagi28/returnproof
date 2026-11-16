from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from returnproof.claims import ClaimConflictError, resolve_claim
from returnproof.domain.models import Claim, RecoveryEntry

from tests.test_claims import (
    CSRF_RE,
    create_claim,
    login,
    ready_return,
    session,
    tenant,
)


def test_claim_lifecycle_deadline_amounts_and_recovery_entry(client: TestClient) -> None:
    _org, _user, token, _order, ret = ready_return(client)
    created = create_claim(client, token, ret.id, template_code="generic").json()
    claim_id = created["id"]

    filed = client.post(
        f"/api/v1/claims/{claim_id}/file",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert filed.status_code == 200, filed.text
    filed_body = filed.json()
    assert filed_body["status"] == "filed"
    filed_at = datetime.fromisoformat(filed_body["filed_at"])
    deadline = datetime.fromisoformat(filed_body["deadline_at"])
    assert deadline - filed_at == timedelta(days=15)

    refile = client.post(
        f"/api/v1/claims/{claim_id}/file",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert refile.status_code == 409

    too_high = client.post(
        f"/api/v1/claims/{claim_id}/resolve",
        headers={"Authorization": f"Bearer {token}"},
        json={"status": "partial", "amount_approved_paise": 249900},
    )
    assert too_high.status_code == 409

    partial = client.post(
        f"/api/v1/claims/{claim_id}/resolve",
        headers={"Authorization": f"Bearer {token}"},
        json={"status": "partial", "amount_approved_paise": 100000},
    )
    assert partial.status_code == 200
    assert partial.json()["status"] == "partial"
    assert partial.json()["amount_approved_paise"] == 100000

    again = client.post(
        f"/api/v1/claims/{claim_id}/resolve",
        headers={"Authorization": f"Bearer {token}"},
        json={"status": "approved"},
    )
    assert again.status_code == 409

    db = session(client)
    try:
        entries = db.query(RecoveryEntry).filter_by(claim_id=claim_id).all()
        assert len(entries) == 1
        assert entries[0].amount_paise == 100000
        assert entries[0].category == "claim_payout"
        claim = db.get(Claim, claim_id)
        assert claim is not None
        assert claim.resolved_at is not None
    finally:
        db.close()


def test_rejected_claim_sets_zero_and_rules_are_enforced(client: TestClient) -> None:
    _org, _user, token, _order, ret = ready_return(client)
    claim_id = create_claim(client, token, ret.id).json()["id"]
    client.post(f"/api/v1/claims/{claim_id}/file", headers={"Authorization": f"Bearer {token}"})
    rejected = client.post(
        f"/api/v1/claims/{claim_id}/resolve",
        headers={"Authorization": f"Bearer {token}"},
        json={"status": "rejected"},
    )
    assert rejected.status_code == 200
    assert rejected.json()["amount_approved_paise"] == 0

    db = session(client)
    try:
        claim = db.get(Claim, claim_id)
        assert claim is not None
        with pytest.raises(ClaimConflictError):
            resolve_claim(db, claim, status="approved")
    finally:
        db.close()


def test_claim_tenant_isolation(client: TestClient) -> None:
    _alpha, _u, alpha_token, _order, ret = ready_return(client, "alpha")
    _beta, _bu, beta_token = tenant(client, "beta")
    claim_id = create_claim(client, alpha_token, ret.id).json()["id"]

    headers = {"Authorization": f"Bearer {beta_token}"}
    assert client.get(f"/api/v1/claims/{claim_id}", headers=headers).status_code == 404
    assert (
        client.get(f"/api/v1/claims/{claim_id}/pack?format=pdf", headers=headers).status_code
        == 404
    )
    assert client.post(f"/api/v1/claims/{claim_id}/file", headers=headers).status_code == 404
    assert client.get("/api/v1/claims", headers=headers).json() == []


def test_ui_claim_creation_and_filing(client: TestClient) -> None:
    _org, user, _token, order, ret = ready_return(client)
    login(client, user.email)
    page = client.get(f"/app/orders/{order.id}")
    assert "Build claim / dispute pack" in page.text
    csrf = CSRF_RE.search(page.text)
    assert csrf is not None

    created = client.post(
        f"/app/returns/{ret.id}/claims",
        data={
            "csrf": csrf.group(1),
            "template_code": "flipkart",
            "amount_rupees": "1999.50",
            "notes": "Escalate with warehouse photos",
        },
        follow_redirects=False,
    )
    assert created.status_code == 303
    assert created.headers["location"].startswith("/app/claims/")
    claim_page = client.get(created.headers["location"])
    assert claim_page.status_code == 200
    assert "Download PDF" in claim_page.text
    assert "flipkart" in claim_page.text
    assert "1999.50" in claim_page.text or "1,999.50" in claim_page.text

    csrf = CSRF_RE.search(claim_page.text)
    assert csrf is not None
    filed = client.post(
        f"{created.headers['location']}/file",
        data={"csrf": csrf.group(1)},
        follow_redirects=False,
    )
    assert filed.status_code == 303
    after = client.get(created.headers["location"])
    assert "Claim filed" in after.text
    assert "Filed" in after.text


def test_claim_pack_escapes_internal_notes(client: TestClient) -> None:
    _org, _user, token, _order, ret = ready_return(client)
    claim = create_claim(
        client,
        token,
        ret.id,
        notes="<script>alert('x')</script>",
    ).json()
    html = client.get(
        f"/api/v1/claims/{claim['id']}/pack?format=html",
        headers={"Authorization": f"Bearer {token}"},
    ).text
    assert "<script>alert('x')</script>" not in html
    assert "&lt;script&gt;" in html
