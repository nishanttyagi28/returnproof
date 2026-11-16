"""Versioned JSON API, authenticated with organisation API keys."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from returnproof.api.schemas import (
    ApiKeyCreate,
    ApiKeyCreated,
    OrderOut,
    OrganisationOut,
    PrincipalOut,
    ReturnOut,
)
from returnproof.auth import Principal, authenticate_api_key, generate_api_key, require_api_roles
from returnproof.db import get_db
from returnproof.domain.enums import Role, VerificationStatus
from returnproof.domain.models import ApiKey
from returnproof.repository import (
    get_order,
    get_organisation,
    get_return,
    list_orders,
    list_returns,
)

router = APIRouter(prefix="/api/v1", tags=["api"])

Db = Annotated[Session, Depends(get_db)]
PrincipalDep = Annotated[Principal, Depends(authenticate_api_key)]


@router.get("/me", response_model=PrincipalOut, summary="Current API principal")
def me(principal: PrincipalDep, db: Db) -> PrincipalOut:
    org = get_organisation(db, principal.org_id)
    if org is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Organisation missing")
    return PrincipalOut(
        org_id=principal.org_id,
        role=principal.role,
        auth_method=principal.auth_method,
        organisation=OrganisationOut.model_validate(org),
    )


@router.get("/orders", response_model=list[OrderOut], summary="List organisation orders")
def orders(
    principal: PrincipalDep,
    db: Db,
    order_status: Annotated[str | None, Query(alias="status")] = None,
    marketplace_id: Annotated[int | None, Query(ge=1)] = None,
    search: Annotated[str | None, Query(max_length=80)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[OrderOut]:
    rows = list_orders(
        db,
        principal.org_id,
        status=order_status,
        marketplace_id=marketplace_id,
        search=search,
        limit=limit,
        offset=offset,
    )
    return [OrderOut.model_validate(row) for row in rows]


@router.get("/orders/{order_id}", response_model=OrderOut, summary="Get one order")
def order_detail(order_id: int, principal: PrincipalDep, db: Db) -> OrderOut:
    order = get_order(db, principal.org_id, order_id)
    if order is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")
    return OrderOut.model_validate(order)


@router.get("/returns", response_model=list[ReturnOut], summary="List returns")
def returns(
    principal: PrincipalDep,
    db: Db,
    return_status: Annotated[str | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[ReturnOut]:
    rows = list_returns(db, principal.org_id, status=return_status, limit=limit, offset=offset)
    return [ReturnOut.model_validate(row) for row in rows]


@router.get("/returns/{return_id}", response_model=ReturnOut, summary="Get one return")
def return_detail(return_id: int, principal: PrincipalDep, db: Db) -> ReturnOut:
    record = get_return(db, principal.org_id, return_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Return not found")
    return ReturnOut.model_validate(record)


@router.get("/verification-statuses", summary="Verification status vocabulary")
def verification_statuses() -> dict[str, list[str]]:
    return {"statuses": [item.value for item in VerificationStatus]}


@router.post(
    "/api-keys",
    response_model=ApiKeyCreated,
    status_code=status.HTTP_201_CREATED,
    summary="Create an API key (owner only)",
)
def create_api_key(
    payload: ApiKeyCreate,
    principal: Annotated[Principal, Depends(require_api_roles(Role.OWNER))],
    db: Db,
) -> ApiKeyCreated:
    token, prefix, key_hash = generate_api_key()
    key = ApiKey(
        org_id=principal.org_id,
        created_by_id=principal.user_id,
        name=payload.name,
        prefix=prefix,
        key_hash=key_hash,
    )
    db.add(key)
    db.commit()
    db.refresh(key)
    return ApiKeyCreated(id=key.id, name=key.name, prefix=key.prefix, token=token)
