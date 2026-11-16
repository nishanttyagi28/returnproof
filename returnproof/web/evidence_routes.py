"""Evidence upload, gallery files and tamper-check pages."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.responses import Response

from returnproof.audit import audit_and_commit
from returnproof.auth import csrf_token, get_current_user, validate_csrf
from returnproof.db import get_db
from returnproof.domain.enums import Role
from returnproof.domain.models import Evidence, User
from returnproof.evidence import EvidenceValidationError, append_evidence, verify_order_chain
from returnproof.repository import get_order
from returnproof.storage import LocalDiskStorage, StorageError, build_storage
from returnproof.web.templating import render

router = APIRouter()


def _require_editor(user: User | None) -> User:
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/login"}
        )
    if user.role not in {Role.OWNER.value, Role.OPS.value}:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Viewers cannot upload evidence",
        )
    return user


@router.post("/app/orders/{order_id}/evidence", include_in_schema=False)
async def upload_evidence_page(
    order_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    file: Annotated[UploadFile, File()],
    user: Annotated[User | None, Depends(get_current_user)] = None,
    stage: Annotated[str, Form()] = "",
    kind: Annotated[str, Form()] = "",
    device_note: Annotated[str, Form(max_length=240)] = "",
    return_request_id: Annotated[str, Form()] = "",
    csrf: Annotated[str, Form()] = "",
) -> Response:
    current = _require_editor(user)
    validate_csrf(request, csrf)
    order = get_order(db, current.org_id, order_id)
    if order is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")
    if file is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="File is required")
    settings = request.app.state.settings
    data = await file.read(settings.max_upload_mb * 1024 * 1024 + 1)
    try:
        evidence = append_evidence(
            db,
            build_storage(settings),
            order=order,
            stage=stage,
            kind=kind,
            filename=file.filename or "upload",
            content_type=file.content_type or "application/octet-stream",
            data=data,
            uploader_id=current.id,
            uploader_label=current.full_name,
            device_note=device_note,
            return_request_id=(
                int(return_request_id) if return_request_id.strip().isdigit() else None
            ),
            max_bytes=settings.max_upload_mb * 1024 * 1024,
        )
        audit_and_commit(
            db,
            org_id=current.org_id,
            actor_user_id=current.id,
            entity_type="evidence",
            entity_id=evidence.id,
            action="evidence.uploaded",
            summary=f"Evidence {evidence.filename} added at {evidence.stage}",
            data={"sha256": evidence.sha256, "chain_hash": evidence.chain_hash},
            ip_address=request.client.host if request.client else None,
        )
        request.session["flash"] = "Evidence added to the order hash chain."
    except (EvidenceValidationError, StorageError) as exc:
        request.session["flash_error"] = str(exc)
    return RedirectResponse(
        f"/app/orders/{order_id}#evidence", status_code=status.HTTP_303_SEE_OTHER
    )


@router.get("/app/orders/{order_id}/evidence/check", include_in_schema=False)
def evidence_check_page(
    order_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User | None, Depends(get_current_user)] = None,
) -> Response:
    if user is None:
        return RedirectResponse(
            f"/login?next=/app/orders/{order_id}/evidence/check",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    order = get_order(db, user.org_id, order_id)
    if order is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")
    report = verify_order_chain(db, build_storage(request.app.state.settings), user.org_id, order_id)
    return render(
        request,
        "evidence_check.html",
        {
            "settings": request.app.state.settings,
            "user": user,
            "order": order,
            "report": report,
            "csrf": csrf_token(request),
        },
    )


@router.get("/app/evidence/{evidence_id}/file", include_in_schema=False)
def evidence_file(
    evidence_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User | None, Depends(get_current_user)] = None,
) -> Response:
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/login"}
        )
    evidence = db.scalar(
        select(Evidence).where(Evidence.id == evidence_id, Evidence.org_id == user.org_id)
    )
    if evidence is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Evidence not found")
    storage = build_storage(request.app.state.settings)
    if not isinstance(storage, LocalDiskStorage):
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="Storage backend unsupported")
    path = storage.local_path(evidence.stored_path)
    if path is None or not path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Evidence file missing")
    return FileResponse(
        path,
        media_type=evidence.content_type,
        filename=evidence.filename,
        content_disposition_type="inline",
    )
