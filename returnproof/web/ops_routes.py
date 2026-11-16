"""Audit log, CSV export downloads and ops pages."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from returnproof.auth import csrf_token, get_current_user
from returnproof.db import get_db
from returnproof.domain.models import User
from returnproof.exports import DATASETS, ExportError, build_export
from returnproof.repository import list_audit_events
from returnproof.web.templating import render

router = APIRouter()


@router.get("/app/audit", include_in_schema=False)
def audit_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User | None, Depends(get_current_user)] = None,
    entity_type: str | None = None,
    action: str | None = None,
) -> Response:
    if user is None:
        return RedirectResponse("/login?next=/app/audit", status_code=status.HTTP_303_SEE_OTHER)
    events = list_audit_events(
        db, user.org_id, entity_type=entity_type, action=action, limit=300
    )
    return render(
        request,
        "audit.html",
        {
            "settings": request.app.state.settings,
            "user": user,
            "events": events,
            "entity_type": entity_type or "",
            "action": action or "",
            "csrf": csrf_token(request),
        },
    )


@router.get("/app/exports/{dataset}.csv", include_in_schema=False)
def export_page(
    dataset: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User | None, Depends(get_current_user)] = None,
) -> Response:
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/login"}
        )
    if dataset not in DATASETS:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown dataset")
    try:
        name, content = build_export(db, user.org_id, dataset)
    except ExportError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    filename = f"{name}_{datetime.now(UTC):%Y%m%d}.csv"
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
