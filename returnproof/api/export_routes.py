"""CSV export endpoints."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import Response
from sqlalchemy.orm import Session

from returnproof.auth import Principal, authenticate_api_key
from returnproof.db import get_db
from returnproof.exports import DATASETS, ExportError, build_export

router = APIRouter(prefix="/api/v1", tags=["exports"])

Db = Annotated[Session, Depends(get_db)]
PrincipalDep = Annotated[Principal, Depends(authenticate_api_key)]


@router.get("/exports/{dataset}.csv", summary="Export an organisation dataset as CSV")
def export_dataset(dataset: str, principal: PrincipalDep, db: Db) -> Response:
    if dataset not in DATASETS:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown dataset")
    try:
        name, content = build_export(db, principal.org_id, dataset)
    except ExportError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    filename = f"{name}_{datetime.now(UTC):%Y%m%d}.csv"
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
