"""Background job submission and status endpoints (in-process runner)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from returnproof.auth import Principal, authenticate_api_key, require_api_roles
from returnproof.claims import generate_claim_pack
from returnproof.config import Settings
from returnproof.db import build_engine, build_session_factory, get_db
from returnproof.domain.enums import Role
from returnproof.domain.models import Evidence, Marketplace, OrderItem, Organisation
from returnproof.repository import get_claim, get_order
from returnproof.settlements import import_settlement
from returnproof.storage import build_storage

router = APIRouter(prefix="/api/v1/jobs", tags=["jobs"])

Db = Annotated[Session, Depends(get_db)]
PrincipalDep = Annotated[Principal, Depends(authenticate_api_key)]
Editor = Annotated[Principal, Depends(require_api_roles(Role.OWNER, Role.OPS))]


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    status: str
    submitted_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    result: dict[str, Any] | None
    error: str | None


def _session(settings: Settings) -> tuple[Any, Session]:
    engine = build_engine(settings.database_url)
    return engine, build_session_factory(engine)()


def run_settlement_import(
    settings: Settings,
    *,
    org_id: int,
    marketplace_id: int,
    file_name: str,
    content: bytes,
    user_id: int | None,
    period_start: str | None,
    period_end: str | None,
) -> dict[str, Any]:
    engine, db = _session(settings)
    try:
        marketplace = db.get(Marketplace, marketplace_id)
        if marketplace is None:
            raise ValueError("Marketplace not found")
        start = datetime.fromisoformat(period_start).replace(tzinfo=UTC) if period_start else None
        end = datetime.fromisoformat(period_end).replace(tzinfo=UTC) if period_end else None
        result = import_settlement(
            db,
            settings,
            org_id=org_id,
            marketplace=marketplace,
            file_name=file_name,
            content=content,
            imported_by_id=user_id,
            period_start=start,
            period_end=end,
        )
        return {
            "settlement_id": result.settlement.id,
            "rows": result.rows,
            "entries_created": result.entries_created,
            "entries_recovered": result.entries_recovered,
            "detected_paise": result.detected_paise,
            "issues": result.issues,
        }
    finally:
        db.close()
        engine.dispose()


def run_claim_pack(settings: Settings, *, org_id: int, claim_id: int) -> dict[str, Any]:
    engine, db = _session(settings)
    try:
        claim = get_claim(db, org_id, claim_id)
        if claim is None:
            raise ValueError("Claim not found")
        order = get_order(db, org_id, claim.order_id)
        if order is None:
            raise ValueError("Claim order not found")
        record = claim.return_request
        if record is None:
            raise ValueError("Claim has no return request")
        organisation = db.get(Organisation, org_id)
        if organisation is None:
            raise ValueError("Organisation not found")
        items = list(
            db.scalars(select(OrderItem).where(OrderItem.order_id == order.id)).all()
        )
        evidence = list(
            db.scalars(
                select(Evidence)
                .where(Evidence.org_id == org_id, Evidence.order_id == order.id)
                .order_by(Evidence.chain_index)
            ).all()
        )
        pack = generate_claim_pack(
            db,
            build_storage(settings),
            settings,
            claim=claim,
            organisation=organisation,
            order=order,
            marketplace=claim.marketplace,
            return_request=record,
            verification=record.verification,
            items=items,
            evidence=evidence,
        )
        return {
            "claim_id": claim.id,
            "pack_path": claim.pack_path,
            "html_path": str(pack.html_path),
            "pdf_path": str(pack.pdf_path),
        }
    finally:
        db.close()
        engine.dispose()


@router.post(
    "/settlement-import",
    response_model=JobOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Queue a settlement CSV import",
)
async def queue_settlement_import(
    request: Request,
    principal: Editor,
    marketplace_id: Annotated[int, Form()],
    file: Annotated[UploadFile, File()],
    period_start: Annotated[str, Form()] = "",
    period_end: Annotated[str, Form()] = "",
) -> JobOut:
    content = await file.read(10 * 1024 * 1024 + 1)
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="CSV exceeds 10 MB"
        )
    job = request.app.state.jobs.submit(
        "settlement_import",
        run_settlement_import,
        request.app.state.settings,
        org_id=principal.org_id,
        marketplace_id=marketplace_id,
        file_name=file.filename or "settlement.csv",
        content=content,
        user_id=principal.user_id,
        period_start=period_start or None,
        period_end=period_end or None,
        owner_org_id=principal.org_id,
    )
    return JobOut.model_validate(job)


@router.post(
    "/claim-pack/{claim_id}",
    response_model=JobOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Queue claim pack regeneration",
)
def queue_claim_pack(
    claim_id: int,
    request: Request,
    db: Db,
    principal: Editor,
) -> JobOut:
    claim = get_claim(db, principal.org_id, claim_id)
    if claim is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Claim not found")
    job = request.app.state.jobs.submit(
        "claim_pack",
        run_claim_pack,
        request.app.state.settings,
        org_id=principal.org_id,
        claim_id=claim_id,
        owner_org_id=principal.org_id,
    )
    return JobOut.model_validate(job)


@router.get("/{job_id}", response_model=JobOut, summary="Get job status")
def job_status(job_id: str, request: Request, principal: PrincipalDep) -> JobOut:
    job = request.app.state.jobs.get(job_id, org_id=principal.org_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    return JobOut.model_validate(job)


@router.get("", response_model=list[JobOut], summary="List recent jobs")
def list_jobs(request: Request, principal: PrincipalDep) -> list[JobOut]:
    jobs = sorted(
        request.app.state.jobs.list_for_org(principal.org_id),
        key=lambda job: job.submitted_at,
        reverse=True,
    )
    return [JobOut.model_validate(job) for job in jobs]
