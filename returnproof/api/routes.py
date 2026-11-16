"""Versioned JSON API, authenticated with organisation API keys."""

from __future__ import annotations

from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

from returnproof.api.schemas import (
    ApiKeyCreate,
    ApiKeyCreated,
    ChainReportOut,
    CsvIssueOut,
    CsvVerificationResult,
    EvidenceOut,
    OrderOut,
    OrganisationOut,
    PrincipalOut,
    ReturnOut,
    VerificationOutFull,
    VerificationRequest,
)
from returnproof.audit import audit_and_commit
from returnproof.auth import Principal, authenticate_api_key, generate_api_key, require_api_roles
from returnproof.db import get_db
from returnproof.domain.enums import Role, VerificationStatus
from returnproof.domain.models import ApiKey, ReturnRequest
from returnproof.evidence import EvidenceValidationError, append_evidence, verify_order_chain
from returnproof.repository import (
    get_order,
    get_order_by_number,
    get_organisation,
    get_return,
    list_evidence,
    list_orders,
    list_returns,
)
from returnproof.storage import StorageError, build_storage
from returnproof.verification import (
    VerificationInput,
    parse_verification_csv,
    verify_return,
)

router = APIRouter(prefix="/api/v1")

Db = Annotated[Session, Depends(get_db)]
PrincipalDep = Annotated[Principal, Depends(authenticate_api_key)]


@router.get(
    "/me", response_model=PrincipalOut, summary="Current API principal", tags=["api-keys"]
)
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


@router.get(
    "/orders", response_model=list[OrderOut], summary="List organisation orders", tags=["orders"]
)
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


@router.get(
    "/orders/{order_id}", response_model=OrderOut, summary="Get one order", tags=["orders"]
)
def order_detail(order_id: int, principal: PrincipalDep, db: Db) -> OrderOut:
    order = get_order(db, principal.org_id, order_id)
    if order is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")
    return OrderOut.model_validate(order)


@router.get(
    "/returns", response_model=list[ReturnOut], summary="List returns", tags=["returns"]
)
def returns(
    principal: PrincipalDep,
    db: Db,
    return_status: Annotated[str | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[ReturnOut]:
    rows = list_returns(db, principal.org_id, status=return_status, limit=limit, offset=offset)
    return [ReturnOut.model_validate(row) for row in rows]


@router.get(
    "/returns/{return_id}", response_model=ReturnOut, summary="Get one return", tags=["returns"]
)
def return_detail(return_id: int, principal: PrincipalDep, db: Db) -> ReturnOut:
    record = get_return(db, principal.org_id, return_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Return not found")
    return ReturnOut.model_validate(record)


@router.get(
    "/verification-statuses", summary="Verification status vocabulary", tags=["verification"]
)
def verification_statuses() -> dict[str, list[str]]:
    return {"statuses": [item.value for item in VerificationStatus]}


@router.post(
    "/api-keys",
    response_model=ApiKeyCreated,
    status_code=status.HTTP_201_CREATED,
    summary="Create an API key (owner only)",
    tags=["api-keys"],
)
def create_api_key(
    payload: ApiKeyCreate,
    request: Request,
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
    audit_and_commit(
        db,
        org_id=principal.org_id,
        actor_user_id=principal.user_id,
        entity_type="api_key",
        entity_id=key.id,
        action="api_key.created",
        summary=f"API key {key.name} created",
        ip_address=request.client.host if request.client else None,
    )
    return ApiKeyCreated(id=key.id, name=key.name, prefix=key.prefix, token=token)


@router.get(
    "/orders/{order_id}/evidence",
    response_model=list[EvidenceOut],
    summary="List evidence on an order",
    tags=["evidence"],
)
def order_evidence(order_id: int, principal: PrincipalDep, db: Db) -> list[EvidenceOut]:
    if get_order(db, principal.org_id, order_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")
    return [EvidenceOut.model_validate(row) for row in list_evidence(db, principal.org_id, order_id)]


@router.post(
    "/orders/{order_id}/evidence",
    response_model=EvidenceOut,
    status_code=status.HTTP_201_CREATED,
    summary="Upload evidence to an order's hash chain",
    tags=["evidence"],
)
async def upload_evidence(
    order_id: int,
    request: Request,
    db: Db,
    principal: Annotated[Principal, Depends(require_api_roles(Role.OWNER, Role.OPS))],
    file: Annotated[UploadFile, File()],
    stage: Annotated[str, Form()],
    kind: Annotated[str, Form()],
    device_note: Annotated[str, Form(max_length=240)] = "",
    return_request_id: Annotated[str, Form()] = "",
) -> EvidenceOut:
    order = get_order(db, principal.org_id, order_id)
    if order is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")
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
            uploader_id=principal.user_id,
            uploader_label=f"api:{principal.api_key_id}",
            device_note=device_note,
            return_request_id=(
                int(return_request_id) if return_request_id.strip().isdigit() else None
            ),
            max_bytes=settings.max_upload_mb * 1024 * 1024,
        )
    except (EvidenceValidationError, StorageError) as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    audit_and_commit(
        db,
        org_id=principal.org_id,
        actor_user_id=principal.user_id,
        entity_type="evidence",
        entity_id=evidence.id,
        action="evidence.uploaded",
        summary=f"Evidence {evidence.filename} added at {evidence.stage}",
        data={"sha256": evidence.sha256, "chain_hash": evidence.chain_hash},
        ip_address=request.client.host if request.client else None,
    )
    return EvidenceOut.model_validate(evidence)


@router.get(
    "/orders/{order_id}/evidence/check",
    response_model=ChainReportOut,
    summary="Recompute and verify an order's evidence hash chain",
    tags=["evidence"],
)
def check_evidence_chain(order_id: int, request: Request, principal: PrincipalDep, db: Db) -> ChainReportOut:
    if get_order(db, principal.org_id, order_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")
    report = verify_order_chain(db, build_storage(request.app.state.settings), principal.org_id, order_id)
    return ChainReportOut.model_validate(report)


def _payload(body: VerificationRequest) -> VerificationInput:
    return VerificationInput(
        identifiers={
            "serial_number": body.serial_number,
            "imei": body.imei,
            "barcode": body.barcode,
            "batch_code": body.batch_code,
        },
        seal_intact=body.seal_intact,
        accessories_present=body.accessories_present,
        tags_present=body.tags_present,
        weight_grams=body.weight_grams,
    )


@router.post(
    "/returns/{return_id}/verification",
    response_model=VerificationOutFull,
    summary="Verify a returned unit against dispatch evidence",
    tags=["verification"],
)
def run_verification(
    return_id: int,
    body: VerificationRequest,
    request: Request,
    db: Db,
    principal: Annotated[Principal, Depends(require_api_roles(Role.OWNER, Role.OPS))],
) -> VerificationOutFull:
    record = get_return(db, principal.org_id, return_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Return not found")
    settings = request.app.state.settings
    result = verify_return(
        db,
        return_request=record,
        payload=_payload(body),
        threshold=settings.phash_threshold,
        weight_tolerance_grams=settings.weight_tolerance_grams,
        weight_tolerance_percent=settings.weight_tolerance_percent,
        storage=build_storage(settings),
        verifier_id=principal.user_id,
    )
    audit_and_commit(
        db,
        org_id=principal.org_id,
        actor_user_id=principal.user_id,
        entity_type="verification",
        entity_id=result.id,
        action="verification.completed",
        summary=f"Return {result.return_request_id} verified as {result.status}",
        data={"score": result.score, "image_distance": result.image_distance},
        ip_address=request.client.host if request.client else None,
    )
    return VerificationOutFull.model_validate(result)


@router.post(
    "/verifications/import",
    response_model=CsvVerificationResult,
    summary="Import return identifier checklist CSV and run verification",
    tags=["verification"],
)
async def import_verification_csv(
    request: Request,
    db: Db,
    principal: Annotated[Principal, Depends(require_api_roles(Role.OWNER, Role.OPS))],
    file: Annotated[UploadFile, File()],
) -> CsvVerificationResult:
    raw = await file.read(5 * 1024 * 1024 + 1)
    if len(raw) > 5 * 1024 * 1024:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="CSV exceeds 5 MB"
        )
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="CSV must be UTF-8"
        ) from exc
    rows, issues = parse_verification_csv(text)
    settings = request.app.state.settings
    results: list[VerificationOutFull] = []
    errors = [CsvIssueOut(row_number=issue.row_number, message=issue.message) for issue in issues]
    for row in rows:
        record = None
        if row.rma_number:
            record = db.scalar(
                select(ReturnRequest)
                .where(
                    ReturnRequest.org_id == principal.org_id,
                    ReturnRequest.rma_number == row.rma_number,
                )
                .order_by(ReturnRequest.id.desc())
            )
        if record is None and row.order_number:
            order = get_order_by_number(db, principal.org_id, row.order_number)
            if order is not None:
                record = db.scalar(
                    select(ReturnRequest)
                    .where(ReturnRequest.order_id == order.id)
                    .order_by(ReturnRequest.id.desc())
                )
        if record is None:
            errors.append(
                CsvIssueOut(
                    row_number=row.row_number,
                    message=f"No return found for {row.rma_number or row.order_number}",
                )
            )
            continue
        try:
            result = verify_return(
                db,
                return_request=record,
                payload=row.payload,
                threshold=settings.phash_threshold,
                weight_tolerance_grams=settings.weight_tolerance_grams,
                weight_tolerance_percent=settings.weight_tolerance_percent,
                storage=build_storage(settings),
                verifier_id=principal.user_id,
            )
            results.append(VerificationOutFull.model_validate(result))
        except ValueError as exc:
            errors.append(CsvIssueOut(row_number=row.row_number, message=str(exc)))
    return CsvVerificationResult(processed=len(results), results=results, errors=errors)
