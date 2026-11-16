"""Claim pack UI pages and lifecycle actions."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session
from starlette.responses import Response

from returnproof.auth import csrf_token, get_current_user, validate_csrf
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
from returnproof.domain.models import Organisation, User
from returnproof.repository import get_claim, get_order, get_return, list_claims
from returnproof.storage import build_storage
from returnproof.web.templating import render

router = APIRouter()


def _editor(user: User | None) -> User:
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/login"}
        )
    if user.role not in {Role.OWNER.value, Role.OPS.value}:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Viewers cannot change claims"
        )
    return user


def _rupees_to_paise(value: str) -> int | None:
    stripped = (value or "").strip()
    if not stripped:
        return None
    try:
        return int((Decimal(stripped) * 100).quantize(Decimal("1")))
    except InvalidOperation as exc:
        raise ClaimValidationError("Amount must be a number") from exc


@router.get("/app/claims", include_in_schema=False)
def claims_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User | None, Depends(get_current_user)] = None,
    claim_status: str | None = None,
) -> Response:
    if user is None:
        return RedirectResponse("/login?next=/app/claims", status_code=status.HTTP_303_SEE_OTHER)
    rows = list_claims(db, user.org_id, status=claim_status)
    return render(
        request,
        "claims.html",
        {
            "settings": request.app.state.settings,
            "user": user,
            "claims": rows,
            "claim_status": claim_status or "",
            "csrf": csrf_token(request),
        },
    )


@router.get("/app/claims/{claim_id}", include_in_schema=False)
def claim_detail_page(
    claim_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User | None, Depends(get_current_user)] = None,
) -> Response:
    if user is None:
        return RedirectResponse(
            f"/login?next=/app/claims/{claim_id}", status_code=status.HTTP_303_SEE_OTHER
        )
    claim = get_claim(db, user.org_id, claim_id)
    if claim is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Claim not found")
    return render(
        request,
        "claim_detail.html",
        {
            "settings": request.app.state.settings,
            "user": user,
            "claim": claim,
            "order": get_order(db, user.org_id, claim.order_id),
            "can_edit": user.role in {Role.OWNER.value, Role.OPS.value},
            "flash": request.session.pop("flash", None),
            "flash_error": request.session.pop("flash_error", None),
            "csrf": csrf_token(request),
        },
    )


@router.post("/app/returns/{return_id}/claims", include_in_schema=False)
def create_claim_page(
    return_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User | None, Depends(get_current_user)] = None,
    csrf: Annotated[str, Form()] = "",
    template_code: Annotated[str, Form()] = "",
    amount_rupees: Annotated[str, Form()] = "",
    notes: Annotated[str, Form(max_length=2000)] = "",
) -> Response:
    current = _editor(user)
    validate_csrf(request, csrf)
    record = get_return(db, current.org_id, return_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Return not found")
    organisation = db.get(Organisation, current.org_id)
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
            template_code=template_code or None,
            amount_claimed_paise=_rupees_to_paise(amount_rupees),
            notes=notes or None,
            actor_id=current.id,
        )
    except (ClaimConflictError, ClaimValidationError) as exc:
        request.session["flash_error"] = str(exc)
        return RedirectResponse(
            f"/app/orders/{record.order_id}#returns", status_code=status.HTTP_303_SEE_OTHER
        )
    request.session["flash"] = f"Claim {claim.reference} created with HTML and PDF packs."
    return RedirectResponse(f"/app/claims/{claim.id}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/app/claims/{claim_id}/file", include_in_schema=False)
def file_claim_page(
    claim_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User | None, Depends(get_current_user)] = None,
    csrf: Annotated[str, Form()] = "",
) -> Response:
    current = _editor(user)
    validate_csrf(request, csrf)
    claim = get_claim(db, current.org_id, claim_id)
    if claim is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Claim not found")
    try:
        updated = file_claim(db, claim)
        request.session["flash"] = (
            f"Claim filed. Deadline {updated.deadline_at:%d %b %Y} (IST)."
        )
    except ClaimConflictError as exc:
        request.session["flash_error"] = str(exc)
    return RedirectResponse(f"/app/claims/{claim_id}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/app/claims/{claim_id}/resolve", include_in_schema=False)
def resolve_claim_page(
    claim_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User | None, Depends(get_current_user)] = None,
    csrf: Annotated[str, Form()] = "",
    claim_status: Annotated[str, Form()] = "",
    amount_rupees: Annotated[str, Form()] = "",
) -> Response:
    current = _editor(user)
    validate_csrf(request, csrf)
    claim = get_claim(db, current.org_id, claim_id)
    if claim is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Claim not found")
    try:
        resolve_claim(
            db,
            claim,
            status=claim_status,
            amount_approved_paise=_rupees_to_paise(amount_rupees),
        )
        request.session["flash"] = f"Claim marked {claim_status}."
    except (ClaimConflictError, ClaimValidationError) as exc:
        request.session["flash_error"] = str(exc)
    return RedirectResponse(f"/app/claims/{claim_id}", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/app/claims/{claim_id}/pack", include_in_schema=False)
def claim_pack_page(
    claim_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User | None, Depends(get_current_user)] = None,
    format: Annotated[str, Query(pattern="^(html|pdf)$")] = "html",
) -> Response:
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": f"/login?next=/app/claims/{claim_id}"},
        )
    claim = get_claim(db, user.org_id, claim_id)
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


@router.get("/app/claim-templates", include_in_schema=False)
def claim_template_options(
    request: Request,
    user: Annotated[User | None, Depends(get_current_user)] = None,
) -> Response:
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/login"}
        )
    return render(
        request,
        "claim_templates.html",
        {
            "settings": request.app.state.settings,
            "user": user,
            "templates": load_claim_templates(),
            "csrf": csrf_token(request),
        },
    )
