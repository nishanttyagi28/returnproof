"""Settlement import and recovery report UI."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from starlette.responses import Response

from returnproof.audit import audit_and_commit
from returnproof.auth import csrf_token, get_current_user, validate_csrf
from returnproof.db import get_db
from returnproof.domain.enums import Role
from returnproof.domain.models import Marketplace, RecoveryEntry, User
from returnproof.repository import (
    get_settlement,
    list_marketplaces,
    list_recoveries,
    list_settlements,
)
from returnproof.settlements import (
    SettlementConflict,
    SettlementError,
    import_settlement,
    mark_recovered,
    reconcile_claims,
    recovery_summary,
)
from returnproof.web.templating import render

router = APIRouter()


def _editor(user: User | None) -> User:
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/login"}
        )
    if user.role not in {Role.OWNER.value, Role.OPS.value}:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Viewers cannot change settlements"
        )
    return user


def _date(value: str) -> datetime | None:
    stripped = (value or "").strip()
    if not stripped:
        return None
    try:
        return datetime.fromisoformat(stripped).replace(tzinfo=UTC)
    except ValueError:
        return None


@router.get("/app/settlements", include_in_schema=False)
def settlements_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User | None, Depends(get_current_user)] = None,
) -> Response:
    if user is None:
        return RedirectResponse(
            "/login?next=/app/settlements", status_code=status.HTTP_303_SEE_OTHER
        )
    return render(
        request,
        "settlements.html",
        {
            "settings": request.app.state.settings,
            "user": user,
            "settlements": list_settlements(db, user.org_id),
            "marketplaces": list_marketplaces(db),
            "can_edit": user.role in {Role.OWNER.value, Role.OPS.value},
            "flash": request.session.pop("flash", None),
            "flash_error": request.session.pop("flash_error", None),
            "csrf": csrf_token(request),
        },
    )


@router.post("/app/settlements/import", include_in_schema=False)
async def import_settlement_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    file: Annotated[UploadFile, File()],
    user: Annotated[User | None, Depends(get_current_user)] = None,
    csrf: Annotated[str, Form()] = "",
    marketplace_id: Annotated[str, Form()] = "",
    period_start: Annotated[str, Form()] = "",
    period_end: Annotated[str, Form()] = "",
) -> Response:
    current = _editor(user)
    validate_csrf(request, csrf)
    try:
        marketplace_pk = int(marketplace_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Marketplace is required"
        ) from exc
    marketplace = db.get(Marketplace, marketplace_pk)
    if marketplace is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Marketplace not found")
    raw = await file.read(10 * 1024 * 1024 + 1)
    try:
        result = import_settlement(
            db,
            request.app.state.settings,
            org_id=current.org_id,
            marketplace=marketplace,
            file_name=file.filename or "settlement.csv",
            content=raw,
            imported_by_id=current.id,
            period_start=_date(period_start),
            period_end=_date(period_end),
        )
        audit_and_commit(
            db,
            org_id=current.org_id,
            actor_user_id=current.id,
            entity_type="settlement",
            entity_id=result.settlement.id,
            action="settlement.imported",
            summary=f"Settlement {result.settlement.file_name} imported with {result.rows} rows",
            data={"detected_paise": result.detected_paise},
            ip_address=request.client.host if request.client else None,
        )
        request.session["flash"] = (
            f"Imported {result.rows} settlement rows; "
            f"{result.entries_created} leakage entries detected."
        )
    except (SettlementConflict, SettlementError) as exc:
        request.session["flash_error"] = str(exc)
    return RedirectResponse("/app/settlements", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/app/settlements/{settlement_id}", include_in_schema=False)
def settlement_detail_page(
    settlement_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User | None, Depends(get_current_user)] = None,
) -> Response:
    if user is None:
        return RedirectResponse(
            f"/login?next=/app/settlements/{settlement_id}",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    settlement = get_settlement(db, user.org_id, settlement_id)
    if settlement is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Settlement not found")
    return render(
        request,
        "settlement_detail.html",
        {
            "settings": request.app.state.settings,
            "user": user,
            "settlement": settlement,
            "csrf": csrf_token(request),
        },
    )


@router.get("/app/recovery", include_in_schema=False)
def recovery_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User | None, Depends(get_current_user)] = None,
    month: str | None = None,
    category: str | None = None,
    entry_status: str | None = None,
) -> Response:
    if user is None:
        return RedirectResponse("/login?next=/app/recovery", status_code=status.HTTP_303_SEE_OTHER)
    summary = recovery_summary(db, user.org_id, month=month)
    entries = list_recoveries(
        db,
        user.org_id,
        category=category,
        status=entry_status,
        limit=300,
    )
    return render(
        request,
        "recovery.html",
        {
            "settings": request.app.state.settings,
            "user": user,
            "summary": summary,
            "entries": entries,
            "month": month or "",
            "category": category or "",
            "entry_status": entry_status or "",
            "can_edit": user.role in {Role.OWNER.value, Role.OPS.value},
            "flash": request.session.pop("flash", None),
            "flash_error": request.session.pop("flash_error", None),
            "csrf": csrf_token(request),
        },
    )


@router.post("/app/recovery/reconcile", include_in_schema=False)
def reconcile_claims_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User | None, Depends(get_current_user)] = None,
    csrf: Annotated[str, Form()] = "",
) -> Response:
    current = _editor(user)
    validate_csrf(request, csrf)
    report = reconcile_claims(db, current.org_id)
    audit_and_commit(
        db,
        org_id=current.org_id,
        actor_user_id=current.id,
        entity_type="recovery",
        entity_id="claims",
        action="recovery.claims_reconciled",
        summary="Approved claims reconciled against settlement payouts",
        data={"entries_created": report.entries_created, "recovered": report.entries_recovered},
        ip_address=request.client.host if request.client else None,
    )
    request.session["flash"] = (
        f"Claim payout reconciliation: {report.entries_created} new at-risk entries, "
        f"{report.entries_recovered} recovered."
    )
    return RedirectResponse("/app/recovery", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/app/recovery/entries/{entry_id}/recover", include_in_schema=False)
def recover_entry_page(
    entry_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User | None, Depends(get_current_user)] = None,
    csrf: Annotated[str, Form()] = "",
) -> Response:
    current = _editor(user)
    validate_csrf(request, csrf)
    entry = db.get(RecoveryEntry, entry_id)
    if entry is None or entry.org_id != current.org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Recovery entry not found")
    try:
        mark_recovered(db, entry)
        audit_and_commit(
            db,
            org_id=current.org_id,
            actor_user_id=current.id,
            entity_type="recovery_entry",
            entity_id=entry.id,
            action="recovery.marked_recovered",
            summary=f"Recovery entry {entry.id} marked recovered",
            data={"amount_paise": entry.amount_paise},
            ip_address=request.client.host if request.client else None,
        )
        request.session["flash"] = "Recovery entry marked recovered."
    except SettlementError as exc:
        request.session["flash_error"] = str(exc)
    return RedirectResponse("/app/recovery", status_code=status.HTTP_303_SEE_OTHER)
