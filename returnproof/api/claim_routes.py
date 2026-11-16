"""Claim pack and lifecycle API."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, HTMLResponse
from sqlalchemy.orm import Session

from returnproof.api.schemas import (
    ClaimCreate,
    ClaimOut,
    ClaimResolve,
    ClaimTemplateOut,
)
from returnproof.audit import audit_and_commit
from returnproof.auth import Principal, authenticate_api_key, require_api_roles
from returnproof.claims import (
    ClaimConflictError,
    ClaimValidationError,
    create_claim,
    file_claim,
    load_claim_templates,
    pack_paths,
    resolve_claim,
)
from returnproof.db import get_db
from returnproof.domain.enums import Role
from returnproof.domain.models import Organisation
from returnproof.repository import get_claim, get_return, list_claims
from returnproof.storage import build_storage

router = APIRouter(prefix="/api/v1", tags=["claims"])

Db = Annotated[Session, Depends(get_db)]
PrincipalDep = Annotated[Principal, Depends(authenticate_api_key)]
Editor = Annotated[Principal, Depends(require_api_roles(Role.OWNER, Role.OPS))]


@router.get(
    "/claim-templates",
    response_model=list[ClaimTemplateOut],
    summary="List data-driven claim templates",
)
def claim_templates() -> list[ClaimTemplateOut]:
    return [
        ClaimTemplateOut(
            code=code,
            name=str(template["name"]),
            title=str(template["title"]),
            subtitle=str(template["subtitle"]),
            sections=[str(section) for section in template["sections"]],
        )
        for code, template in load_claim_templates().items()
    ]


@router.post(
    "/returns/{return_id}/claims",
    response_model=ClaimOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a claim and generate its evidence pack",
)
def create_return_claim(
    return_id: int,
    body: ClaimCreate,
    request: Request,
    db: Db,
    principal: Editor,
) -> ClaimOut:
    record = get_return(db, principal.org_id, return_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Return not found")
    organisation = db.get(Organisation, principal.org_id)
    if organisation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organisation not found")
    settings = request.app.state.settings
    try:
        claim, _pack = create_claim(
            db,
            build_storage(settings),
            settings,
            organisation=organisation,
            order=record.order,
            return_request=record,
            verification=record.verification,
            marketplace=record.order.marketplace,
            template_code=body.template_code,
            amount_claimed_paise=body.amount_claimed_paise,
            notes=body.notes,
            actor_id=principal.user_id,
        )
    except (ClaimConflictError, ClaimValidationError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    audit_and_commit(
        db,
        org_id=principal.org_id,
        actor_user_id=principal.user_id,
        entity_type="claim",
        entity_id=claim.id,
        action="claim.created",
        summary=f"Claim {claim.reference} created with {claim.template_code} pack",
        ip_address=request.client.host if request.client else None,
    )
    return ClaimOut.model_validate(claim)


@router.get("/claims", response_model=list[ClaimOut], summary="List claims")
def claims(
    principal: PrincipalDep,
    db: Db,
    claim_status: Annotated[str | None, Query(alias="status")] = None,
) -> list[ClaimOut]:
    return [
        ClaimOut.model_validate(row)
        for row in list_claims(db, principal.org_id, status=claim_status)
    ]


@router.get("/claims/{claim_id}", response_model=ClaimOut, summary="Get one claim")
def claim_detail(claim_id: int, principal: PrincipalDep, db: Db) -> ClaimOut:
    claim = get_claim(db, principal.org_id, claim_id)
    if claim is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Claim not found")
    return ClaimOut.model_validate(claim)


@router.post("/claims/{claim_id}/file", response_model=ClaimOut, summary="File a draft claim")
def file_claim_endpoint(
    claim_id: int, request: Request, principal: Editor, db: Db
) -> ClaimOut:
    claim = get_claim(db, principal.org_id, claim_id)
    if claim is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Claim not found")
    try:
        updated = file_claim(db, claim, filed_at=datetime.now(UTC))
    except ClaimConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    audit_and_commit(
        db,
        org_id=principal.org_id,
        actor_user_id=principal.user_id,
        entity_type="claim",
        entity_id=updated.id,
        action="claim.filed",
        summary=f"Claim {updated.reference} filed",
        ip_address=request.client.host if request.client else None,
    )
    return ClaimOut.model_validate(updated)


@router.post(
    "/claims/{claim_id}/resolve", response_model=ClaimOut, summary="Resolve a filed claim"
)
def resolve_claim_endpoint(
    claim_id: int,
    body: ClaimResolve,
    request: Request,
    principal: Editor,
    db: Db,
) -> ClaimOut:
    claim = get_claim(db, principal.org_id, claim_id)
    if claim is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Claim not found")
    try:
        updated = resolve_claim(
            db,
            claim,
            status=body.status,
            amount_approved_paise=body.amount_approved_paise,
            resolved_at=datetime.now(UTC),
        )
    except (ClaimConflictError, ClaimValidationError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    audit_and_commit(
        db,
        org_id=principal.org_id,
        actor_user_id=principal.user_id,
        entity_type="claim",
        entity_id=updated.id,
        action="claim.resolved",
        summary=f"Claim {updated.reference} resolved as {updated.status}",
        data={"amount_approved_paise": updated.amount_approved_paise},
        ip_address=request.client.host if request.client else None,
    )
    return ClaimOut.model_validate(updated)


@router.get("/claims/{claim_id}/pack", summary="View or download a claim pack")
def claim_pack(
    claim_id: int,
    request: Request,
    principal: PrincipalDep,
    db: Db,
    format: Annotated[str, Query(pattern="^(html|pdf)$")] = "pdf",
) -> object:
    claim = get_claim(db, principal.org_id, claim_id)
    if claim is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Claim not found")
    if not claim.pack_path:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pack not generated")
    html_path, pdf_path = pack_paths(request.app.state.settings, claim)
    target = html_path if format == "html" else pdf_path
    if not target.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pack file missing")
    if format == "html":
        return HTMLResponse(target.read_text(encoding="utf-8"))
    return FileResponse(target, media_type="application/pdf", filename=f"{claim.reference}.pdf")
