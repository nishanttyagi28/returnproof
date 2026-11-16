from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from fastapi.testclient import TestClient
from returnproof.app import create_app
from returnproof.config import Settings
from returnproof.logging_config import JsonFormatter

from tests.test_claims import create_claim, login, ready_return, session, tenant
from tests.test_settlements import HEADER


def test_audit_log_records_state_changes_and_exports(client: TestClient) -> None:
    _org, user, token, _order, ret = ready_return(client)
    claim = create_claim(client, token, ret.id, template_code="generic").json()
    login(client, user.email)
    page = client.get("/app/audit")
    assert page.status_code == 200
    assert "Audit log" in page.text
    assert "auth.login" in page.text
    assert "evidence.uploaded" in page.text
    assert "verification.completed" in page.text
    assert "claim.created" in page.text

    export = client.get("/app/exports/audit.csv")
    assert export.status_code == 200
    assert export.headers["content-type"].startswith("text/csv")
    assert "auth.login" in export.text
    assert "claim.created" in export.text

    filtered = client.get("/app/audit?entity_type=claim")
    assert claim["reference"] in filtered.text
    assert "evidence.uploaded" not in filtered.text


def test_exports_are_tenant_scoped_and_formula_safe(client: TestClient) -> None:
    _org, _user, token, order, _ret = ready_return(client)
    db = session(client)
    try:
        row = db.get(type(order), order.id)
        assert row is not None
        row.customer_name = "=HYPERLINK(\"http://evil\")"
        db.commit()
    finally:
        db.close()

    response = client.get(
        "/api/v1/exports/orders.csv", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200
    assert order.order_number in response.text
    assert "'=HYPERLINK" in response.text
    assert "total_paise" in response.text

    returns = client.get(
        "/api/v1/exports/returns.csv", headers={"Authorization": f"Bearer {token}"}
    )
    assert returns.status_code == 200
    assert "rma_number" in returns.text

    _beta, _bu, beta_token = tenant(client, "beta")
    beta = client.get(
        "/api/v1/exports/orders.csv", headers={"Authorization": f"Bearer {beta_token}"}
    )
    assert order.order_number not in beta.text
    assert beta.text.strip().splitlines() == [
        "order_number,marketplace,status,customer_name,customer_pincode,placed_at_utc,"
        "dispatched_at_utc,delivered_at_utc,total_paise,currency,payment_method,return_count"
    ]
    assert client.get("/api/v1/exports/secrets.csv", headers={"Authorization": f"Bearer {token}"}).status_code == 404


def test_login_rate_limit_returns_429(client: TestClient) -> None:
    page = client.get("/login")
    import re

    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text)
    assert csrf is not None
    statuses = []
    for _ in range(11):
        response = client.post(
            "/login",
            data={"email": "nobody@example.com", "password": "wrong", "csrf": csrf.group(1)},
            follow_redirects=False,
        )
        statuses.append(response.status_code)
    assert statuses[:10] == [401] * 10
    assert statuses[10] == 429
    assert "Retry-After" in response.headers


def test_error_pages_and_request_ids(client: TestClient) -> None:
    missing = client.get("/app/does-not-exist")
    assert missing.status_code == 404
    assert "Page not found" in missing.text
    assert missing.headers["x-request-id"]
    assert missing.headers["x-request-id"] in missing.text

    api_missing = client.get("/api/v1/does-not-exist")
    assert api_missing.status_code == 404
    assert api_missing.json()["detail"]
    assert api_missing.json()["request_id"] == api_missing.headers["x-request-id"]


def test_unhandled_error_returns_styled_500(settings: Settings) -> None:
    app = create_app(settings)

    @app.get("/boom")
    def boom() -> None:
        raise RuntimeError("deliberate test failure")

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/boom")
        assert response.status_code == 500
        assert "Something went wrong" in response.text
        assert response.headers["x-request-id"]

        api = client.get("/api/v1/boom")
        assert api.status_code == 404


def test_background_jobs_import_and_pack(client: TestClient) -> None:
    _org, _user, token, order, ret = ready_return(client)
    claim = create_claim(client, token, ret.id, template_code="generic").json()

    job = client.post(
        f"/api/v1/jobs/claim-pack/{claim['id']}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert job.status_code == 202
    job_id = job.json()["id"]
    finished = client.app.state.jobs.wait(job_id, timeout=10)
    assert finished is not None
    assert finished.status == "succeeded", finished.error
    status_response = client.get(
        f"/api/v1/jobs/{job_id}", headers={"Authorization": f"Bearer {token}"}
    )
    assert status_response.status_code == 200
    assert status_response.json()["result"]["claim_id"] == claim["id"]

    csv_content = (
        HEADER
        + "\n"
        + f"{order.order_number},sale,NR-1,1,1000.00,120.00,0.00,0.00,0.00,0.00,880.00,0.00\n"
    ).encode()
    import_job = client.post(
        "/api/v1/jobs/settlement-import",
        headers={"Authorization": f"Bearer {token}"},
        data={"marketplace_id": str(order.marketplace_id), "period_start": "2026-11-01"},
        files={"file": ("job.csv", csv_content, "text/csv")},
    )
    assert import_job.status_code == 202
    import_id = import_job.json()["id"]
    finished_import = client.app.state.jobs.wait(import_id, timeout=10)
    assert finished_import is not None
    assert finished_import.status == "succeeded", finished_import.error
    assert finished_import.result is not None
    assert finished_import.result["rows"] == 1

    _beta, _bu, beta_token = tenant(client, "beta")
    hidden = client.get(
        f"/api/v1/jobs/{import_id}", headers={"Authorization": f"Bearer {beta_token}"}
    )
    assert hidden.status_code == 404


def test_openapi_is_grouped_by_tag(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    tag_names = {tag["name"] for tag in schema["tags"]}
    assert {"orders", "returns", "evidence", "verification", "claims", "recovery", "jobs"} <= tag_names
    tags_used = set()
    for path in schema["paths"].values():
        for operation in path.values():
            tags_used.update(operation.get("tags", []))
    assert "evidence" in tags_used
    assert "claims" in tags_used
    assert "recovery" in tags_used
    assert "jobs" in tags_used


def test_structured_log_formatter() -> None:
    record = logging.LogRecord(
        name="returnproof.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="event occurred",
        args=(),
        exc_info=None,
    )
    record.request_id = "abc123"
    record.duration_ms = 12.5
    payload = json.loads(JsonFormatter().format(record))
    assert payload["level"] == "info"
    assert payload["logger"] == "returnproof.test"
    assert payload["message"] == "event occurred"
    assert payload["request_id"] == "abc123"
    assert payload["duration_ms"] == 12.5
    assert "ts" in payload


def test_recovery_audit_and_manual_recovery(client: TestClient) -> None:
    org, user, _token, order, _ret = ready_return(client)
    login(client, user.email)
    # Create a ledger entry directly via the settlement import UI path is covered
    # elsewhere; here we only need an open entry to mark recovered.
    from returnproof.domain.models import RecoveryEntry

    db = session(client)
    try:
        entry = RecoveryEntry(
            org_id=org.id,
            marketplace_id=order.marketplace_id,
            direction="at_risk",
            status="open",
            category="other",
            amount_paise=12345,
            detected_at=datetime.now(UTC),
            description="Manual test leakage",
            is_sample=True,
        )
        db.add(entry)
        db.commit()
        db.refresh(entry)
        entry_id = entry.id
    finally:
        db.close()

    page = client.get("/app/recovery")
    import re

    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text)
    assert csrf is not None
    response = client.post(
        f"/app/recovery/entries/{entry_id}/recover",
        data={"csrf": csrf.group(1)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    audit = client.get("/app/audit?action=recovery.marked_recovered")
    assert "recovery.marked_recovered" in audit.text
