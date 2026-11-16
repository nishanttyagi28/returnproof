"""Settlement import and financial recovery API."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from sqlalchemy.orm import Session

from returnproof.api.schemas import (
    ReconciliationOut,
    RecoveryEntryOut,
    RecoverySummaryOut,
    RecoverySummaryRowOut,
    SettlementDetailOut,
    SettlementImportOut,
    SettlementLineOut,
    SettlementOut,
)
from returnproof.audit import audit_and_commit
from returnproof.auth import Principal, authenticate_api_key, require_api_roles
from returnproof.db import get_db
from returnproof.domain.enums import Role
from returnproof.domain.models import Marketplace, RecoveryEntry
from returnproof.repository import (
    get_settlement,
    list_recoveries,
    list_settlements,
)
from returnproof.settlements import (
    SettlementConflict,
    SettlementError,
    import_settlement,
    mark_recovered,
    reconcile_claims,
    reconcile_settlement,
    recovery_summary,
)

router = APIRouter(prefix="/api/v1", tags=["recovery"])

Db = Annotated[Session, Depends(get_db)]
PrincipalDep = Annotated[Principal, Depends(authenticate_api_key)]
Editor = Annotated[Principal, Depends(require_api_roles(Role.OWNER, Role.OPS))]


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid date {value!r}; use YYYY-MM-DD",
        ) from exc
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


@router.post(
    "/settlements/import",
    response_model=SettlementImportOut,
    status_code=status.HTTP_201_CREATED,
    summary="Import a marketplace settlement CSV and detect leakage",
)
async def import_settlement_endpoint(
    request: Request,
    db: Db,
    principal: Editor,
    marketplace_id: Annotated[int, Form()],
    file: Annotated[UploadFile, File()],
    period_start: Annotated[str, Form()] = "",
    period_end: Annotated[str, Form()] = "",
) -> SettlementImportOut:
    marketplace = db.get(Marketplace, marketplace_id)
    if marketplace is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Marketplace not found")
    raw = await file.read(10 * 1024 * 1024 + 1)
    if len(raw) > 10 * 1024 * 1024:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="CSV exceeds 10 MB"
        )
    try:
        result = import_settlement(
            db,
            request.app.state.settings,
            org_id=principal.org_id,
            marketplace=marketplace,
            file_name=file.filename or "settlement.csv",
            content=raw,
            imported_by_id=principal.user_id,
            period_start=_parse_date(period_start),
            period_end=_parse_date(period_end),
        )
    except SettlementConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except SettlementError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    audit_and_commit(
        db,
        org_id=principal.org_id,
        actor_user_id=principal.user_id,
        entity_type="settlement",
        entity_id=result.settlement.id,
        action="settlement.imported",
        summary=f"Settlement {result.settlement.file_name} imported with {result.rows} rows",
        data={"detected_paise": result.detected_paise},
        ip_address=request.client.host if request.client else None,
    )
    return SettlementImportOut(
        settlement=SettlementOut.model_validate(result.settlement),
        rows=result.rows,
        issues=result.issues,
        entries_created=result.entries_created,
        entries_recovered=result.entries_recovered,
        detected_paise=result.detected_paise,
    )


@router.get("/settlements", response_model=list[SettlementOut], summary="List settlements")
def settlements(principal: PrincipalDep, db: Db) -> list[SettlementOut]:
    return [
        SettlementOut.model_validate(row)
        for row in list_settlements(db, principal.org_id)
    ]


@router.get(
    "/settlements/{settlement_id}",
    response_model=SettlementDetailOut,
    summary="Get a settlement with lines",
)
def settlement_detail(
    settlement_id: int, principal: PrincipalDep, db: Db
) -> SettlementDetailOut:
    settlement = get_settlement(db, principal.org_id, settlement_id)
    if settlement is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Settlement not found")
    payload = SettlementOut.model_validate(settlement).model_dump()
    payload["lines"] = [SettlementLineOut.model_validate(line) for line in settlement.lines]
    return SettlementDetailOut(**payload)


@router.post(
    "/settlements/{settlement_id}/reconcile",
    response_model=ReconciliationOut,
    summary="Re-run settlement reconciliation",
)
def reconcile_endpoint(
    settlement_id: int,
    request: Request,
    db: Db,
    principal: Editor,
) -> ReconciliationOut:
    settlement = get_settlement(db, principal.org_id, settlement_id)
    if settlement is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Settlement not found")
    report = reconcile_settlement(db, settlement, settings=request.app.state.settings)
    audit_and_commit(
        db,
        org_id=principal.org_id,
        actor_user_id=principal.user_id,
        entity_type="settlement",
        entity_id=settlement.id,
        action="settlement.reconciled",
        summary=f"Settlement {settlement.id} reconciled",
        data={"detected_paise": report.detected_paise},
        ip_address=request.client.host if request.client else None,
    )
    return ReconciliationOut(
        settlement_id=report.settlement_id,
        lines_checked=report.lines_checked,
        entries_created=report.entries_created,
        entries_recovered=report.entries_recovered,
        detected_paise=report.detected_paise,
        by_category=report.by_category,
    )


@router.get(
    "/recovery/entries",
    response_model=list[RecoveryEntryOut],
    summary="List recovery ledger entries",
)
def recovery_entries(
    principal: PrincipalDep,
    db: Db,
    direction: Annotated[str | None, Query()] = None,
    entry_status: Annotated[str | None, Query(alias="status")] = None,
    category: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> list[RecoveryEntryOut]:
    rows = list_recoveries(
        db,
        principal.org_id,
        direction=direction,
        status=entry_status,
        category=category,
        limit=limit,
    )
    return [RecoveryEntryOut.model_validate(row) for row in rows]


@router.post(
    "/recovery/reconcile",
    response_model=ReconciliationOut,
    summary="Reconcile approved claims against recorded payouts",
)
def reconcile_claims_endpoint(
    request: Request, db: Db, principal: Editor
) -> ReconciliationOut:
    report = reconcile_claims(db, principal.org_id)
    audit_and_commit(
        db,
        org_id=principal.org_id,
        actor_user_id=principal.user_id,
        entity_type="recovery",
        entity_id="claims",
        action="recovery.claims_reconciled",
        summary="Approved claims reconciled against settlement payouts",
        data={"entries_created": report.entries_created, "recovered": report.entries_recovered},
        ip_address=request.client.host if request.client else None,
    )
    return ReconciliationOut(
        settlement_id=None,
        lines_checked=report.lines_checked,
        entries_created=report.entries_created,
        entries_recovered=report.entries_recovered,
        detected_paise=report.detected_paise,
        by_category=report.by_category,
    )


@router.post(
    "/recovery/entries/{entry_id}/recover",
    response_model=RecoveryEntryOut,
    summary="Mark a recovery entry as recovered",
)
def recover_entry(
    entry_id: int, request: Request, db: Db, principal: Editor
) -> RecoveryEntryOut:
    entry = db.get(RecoveryEntry, entry_id)
    if entry is None or entry.org_id != principal.org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Recovery entry not found")
    try:
        updated = mark_recovered(db, entry)
    except SettlementError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    audit_and_commit(
        db,
        org_id=principal.org_id,
        actor_user_id=principal.user_id,
        entity_type="recovery_entry",
        entity_id=updated.id,
        action="recovery.marked_recovered",
        summary=f"Recovery entry {updated.id} marked recovered",
        data={"amount_paise": updated.amount_paise},
        ip_address=request.client.host if request.client else None,
    )
    return RecoveryEntryOut.model_validate(updated)


@router.get(
    "/recovery/summary",
    response_model=RecoverySummaryOut,
    summary="Money at risk versus recovered, grouped by marketplace and month",
)
def recovery_summary_endpoint(
    principal: PrincipalDep,
    db: Db,
    month: Annotated[str | None, Query(pattern=r"^\d{4}-\d{2}$")] = None,
) -> RecoverySummaryOut:
    summary = recovery_summary(db, principal.org_id, month=month)
    return RecoverySummaryOut(
        rows=[
            RecoverySummaryRowOut(
                marketplace_id=row.marketplace_id,
                marketplace=row.marketplace,
                month=row.month,
                at_risk_paise=row.at_risk_paise,
                recovered_paise=row.recovered_paise,
                open_entries=row.open_entries,
                recovered_entries=row.recovered_entries,
            )
            for row in summary.rows
        ],
        total_at_risk_paise=summary.total_at_risk_paise,
        total_recovered_paise=summary.total_recovered_paise,
    )
