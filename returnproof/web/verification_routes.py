"""UI route for running product-identity verification."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from starlette.responses import Response

from returnproof.auth import get_current_user, validate_csrf
from returnproof.db import get_db
from returnproof.domain.enums import Role
from returnproof.domain.models import User
from returnproof.repository import get_return
from returnproof.storage import build_storage
from returnproof.verification import VerificationInput, verify_return

router = APIRouter()


def _tri_state(value: str) -> bool | None:
    normalized = (value or "").strip().lower()
    if normalized == "yes":
        return True
    if normalized == "no":
        return False
    return None


def _optional_int(value: str) -> int | None:
    stripped = (value or "").strip()
    return int(stripped) if stripped.isdigit() else None


@router.post("/app/returns/{return_id}/verify", include_in_schema=False)
def verify_from_ui(
    return_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User | None, Depends(get_current_user)] = None,
    csrf: Annotated[str, Form()] = "",
    serial_number: Annotated[str, Form(max_length=120)] = "",
    imei: Annotated[str, Form(max_length=32)] = "",
    barcode: Annotated[str, Form(max_length=64)] = "",
    batch_code: Annotated[str, Form(max_length=64)] = "",
    weight_grams: Annotated[str, Form()] = "",
    seal_intact: Annotated[str, Form()] = "",
    accessories_present: Annotated[str, Form()] = "",
    tags_present: Annotated[str, Form()] = "",
) -> Response:
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": "/login?next=/app/orders"},
        )
    if user.role not in {Role.OWNER.value, Role.OPS.value}:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Viewers cannot run verification"
        )
    validate_csrf(request, csrf)
    record = get_return(db, user.org_id, return_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Return not found")
    settings = request.app.state.settings
    try:
        result = verify_return(
            db,
            return_request=record,
            payload=VerificationInput(
                identifiers={
                    "serial_number": serial_number or None,
                    "imei": imei or None,
                    "barcode": barcode or None,
                    "batch_code": batch_code or None,
                },
                seal_intact=_tri_state(seal_intact),
                accessories_present=_tri_state(accessories_present),
                tags_present=_tri_state(tags_present),
                weight_grams=_optional_int(weight_grams),
            ),
            threshold=settings.phash_threshold,
            weight_tolerance_grams=settings.weight_tolerance_grams,
            weight_tolerance_percent=settings.weight_tolerance_percent,
            storage=build_storage(settings),
            verifier_id=user.id,
        )
        request.session["flash"] = (
            f"Verification complete: {result.status.replace('_', ' ').title()} "
            f"(score {result.score:.2f})."
        )
    except ValueError as exc:
        request.session["flash_error"] = str(exc)
    return RedirectResponse(
        f"/app/orders/{record.order_id}#returns", status_code=status.HTTP_303_SEE_OTHER
    )
