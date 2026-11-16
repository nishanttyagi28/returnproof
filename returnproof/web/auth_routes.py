"""Authentication and application pages."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.responses import Response

from returnproof.auth import (
    csrf_token,
    get_current_user,
    login_session,
    logout_session,
    validate_csrf,
    verify_password,
)
from returnproof.db import get_db
from returnproof.domain.models import User, utcnow
from returnproof.web.templating import render

router = APIRouter()


def _safe_next(value: str | None) -> str:
    if value and value.startswith("/") and not value.startswith("//"):
        return value
    return "/app"


@router.get("/login", response_class=HTMLResponse, include_in_schema=False)
def login_page(request: Request, next: str | None = None) -> Response:
    if request.session.get("uid"):
        return RedirectResponse("/app", status_code=status.HTTP_303_SEE_OTHER)
    return render(
        request,
        "login.html",
        {
            "settings": request.app.state.settings,
            "csrf": csrf_token(request),
            "next": _safe_next(next),
            "error": None,
            "email": "",
        },
    )


@router.post("/login", response_class=HTMLResponse, include_in_schema=False, response_model=None)
def login_submit(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    email: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
    csrf: Annotated[str, Form()] = "",
    next: Annotated[str, Form()] = "/app",
) -> Response:
    validate_csrf(request, csrf)
    normalized = email.strip().lower()
    user = db.scalar(select(User).where(User.email == normalized).order_by(User.id))
    if user is None or not verify_password(user.password_hash, password):
        return render(
            request,
            "login.html",
            {
                "settings": request.app.state.settings,
                "csrf": csrf_token(request),
                "next": _safe_next(next),
                "error": "Email or password is incorrect.",
                "email": normalized,
            },
            status_code=status.HTTP_401_UNAUTHORIZED,
        )
    if not user.is_active:
        return render(
            request,
            "login.html",
            {
                "settings": request.app.state.settings,
                "csrf": csrf_token(request),
                "next": _safe_next(next),
                "error": "This account is disabled. Contact your organisation owner.",
                "email": normalized,
            },
            status_code=status.HTTP_403_FORBIDDEN,
        )
    user.last_login_at = utcnow()
    db.commit()
    login_session(request, user)
    return RedirectResponse(_safe_next(next), status_code=status.HTTP_303_SEE_OTHER)


@router.post("/logout", include_in_schema=False)
def logout(request: Request, csrf: Annotated[str, Form()] = "") -> Response:
    validate_csrf(request, csrf)
    logout_session(request)
    return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/app", response_class=HTMLResponse, include_in_schema=False)
def dashboard(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User | None, Depends(get_current_user)] = None,
) -> Response:
    if user is None:
        return RedirectResponse("/login?next=/app", status_code=status.HTTP_303_SEE_OTHER)
    from returnproof.repository import (
        claims_due_soon,
        dashboard_metrics,
        get_organisation,
        list_returns,
    )

    metrics = dashboard_metrics(db, user.org_id)
    recent = list_returns(db, user.org_id)[:6]
    due_claims = claims_due_soon(db, user.org_id, days=7)
    return render(
        request,
        "dashboard.html",
        {
            "settings": request.app.state.settings,
            "user": user,
            "org": get_organisation(db, user.org_id),
            "metrics": metrics,
            "recent_returns": recent,
            "due_claims": due_claims,
            "csrf": csrf_token(request),
        },
    )


@router.get("/app/orders", response_class=HTMLResponse, include_in_schema=False)
def orders_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User | None, Depends(get_current_user)] = None,
    status_filter: str | None = None,
    q: str | None = None,
    marketplace_filter: str | None = None,
) -> Response:
    if user is None:
        return RedirectResponse("/login?next=/app/orders", status_code=status.HTTP_303_SEE_OTHER)
    from returnproof.repository import list_marketplaces, list_orders

    try:
        marketplace_id = int(marketplace_filter) if marketplace_filter else None
    except ValueError:
        marketplace_id = None
    rows = list_orders(
        db,
        user.org_id,
        status=status_filter,
        marketplace_id=marketplace_id,
        search=q,
    )
    return render(
        request,
        "orders.html",
        {
            "settings": request.app.state.settings,
            "user": user,
            "orders": rows,
            "marketplaces": list_marketplaces(db),
            "status_filter": status_filter or "",
            "q": q or "",
            "marketplace_filter": marketplace_filter or "",
            "csrf": csrf_token(request),
        },
    )


@router.get("/app/returns", response_class=HTMLResponse, include_in_schema=False)
def returns_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User | None, Depends(get_current_user)] = None,
    status_filter: str | None = None,
    verification_filter: str | None = None,
    marketplace_filter: str | None = None,
    q: str | None = None,
) -> Response:
    if user is None:
        return RedirectResponse("/login?next=/app/returns", status_code=status.HTTP_303_SEE_OTHER)
    from returnproof.repository import list_marketplaces, list_returns

    try:
        marketplace_id = int(marketplace_filter) if marketplace_filter else None
    except ValueError:
        marketplace_id = None
    rows = list_returns(
        db,
        user.org_id,
        status=status_filter,
        marketplace_id=marketplace_id,
        verification_status=verification_filter,
        search=q,
    )
    return render(
        request,
        "returns.html",
        {
            "settings": request.app.state.settings,
            "user": user,
            "returns": rows,
            "marketplaces": list_marketplaces(db),
            "status_filter": status_filter or "",
            "verification_filter": verification_filter or "",
            "marketplace_filter": marketplace_filter or "",
            "q": q or "",
            "csrf": csrf_token(request),
        },
    )


@router.get("/app/returns/{return_id}", response_class=HTMLResponse, include_in_schema=False)
def return_detail_page(
    return_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User | None, Depends(get_current_user)] = None,
) -> Response:
    if user is None:
        return RedirectResponse(
            f"/login?next=/app/returns/{return_id}", status_code=status.HTTP_303_SEE_OTHER
        )
    from returnproof.repository import get_claim_for_return, get_return, list_evidence

    record = get_return(db, user.org_id, return_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Return not found")
    return render(
        request,
        "return_detail.html",
        {
            "settings": request.app.state.settings,
            "user": user,
            "return_request": record,
            "order": record.order,
            "evidence": list_evidence(db, user.org_id, record.order_id),
            "claim": get_claim_for_return(db, user.org_id, record.id),
            "csrf": csrf_token(request),
        },
    )


@router.get("/app/orders/{order_id}", response_class=HTMLResponse, include_in_schema=False)
def order_detail_page(
    order_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User | None, Depends(get_current_user)] = None,
) -> Response:
    if user is None:
        return RedirectResponse(
            f"/login?next=/app/orders/{order_id}", status_code=status.HTTP_303_SEE_OTHER
        )
    from returnproof.claims import claim_eligible, load_claim_templates
    from returnproof.evidence import verify_order_chain
    from returnproof.repository import get_order, list_evidence
    from returnproof.storage import build_storage

    order = get_order(db, user.org_id, order_id)
    if order is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")
    evidence = list_evidence(db, user.org_id, order.id)
    report = verify_order_chain(db, build_storage(request.app.state.settings), user.org_id, order.id)
    flash = request.session.pop("flash", None)
    flash_error = request.session.pop("flash_error", None)
    return render(
        request,
        "order_detail.html",
        {
            "settings": request.app.state.settings,
            "user": user,
            "order": order,
            "evidence": evidence,
            "chain": report,
            "flash": flash,
            "flash_error": flash_error,
            "can_upload": user.role in {"owner", "ops"},
            "claim_eligible": claim_eligible,
            "claim_templates": load_claim_templates(),
            "csrf": csrf_token(request),
        },
    )
