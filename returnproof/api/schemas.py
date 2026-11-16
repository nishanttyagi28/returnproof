"""Pydantic API schemas."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ApiModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class OrganisationOut(ApiModel):
    id: int
    name: str
    gstin: str | None


class PrincipalOut(ApiModel):
    org_id: int
    role: str
    auth_method: str
    organisation: OrganisationOut


class OrderItemOut(ApiModel):
    id: int
    sku_snapshot: str
    title_snapshot: str
    quantity: int
    unit_price_paise: int
    serial_number: str | None
    imei: str | None
    batch_code: str | None


class VerificationOut(ApiModel):
    status: str
    score: float
    reasons: list[object]
    image_distance: int | None


class ReturnSummaryOut(ApiModel):
    id: int
    rma_number: str
    status: str
    reason_code: str
    requested_at: datetime
    received_at: datetime | None
    refund_amount_paise: int
    verification: VerificationOut | None


class OrderOut(ApiModel):
    id: int
    order_number: str
    status: str
    customer_name: str
    customer_pincode: str | None
    placed_at: datetime
    total_paise: int
    marketplace_id: int
    items: list[OrderItemOut] = Field(default_factory=list)
    returns: list[ReturnSummaryOut] = Field(default_factory=list)


class ReturnOut(ReturnSummaryOut):
    org_id: int
    order_id: int
    buyer_comment: str | None


class EvidenceOut(ApiModel):
    id: int
    return_request_id: int | None
    stage: str
    kind: str
    chain_index: int
    filename: str
    content_type: str
    size_bytes: int
    sha256: str
    previous_hash: str
    chain_hash: str
    captured_at: datetime
    uploader_label: str
    device_note: str | None
    width: int | None
    height: int | None
    perceptual_hash: str | None


class ChainProblemOut(ApiModel):
    evidence_id: int | None
    code: str
    message: str


class ChainReportOut(ApiModel):
    order_id: int
    evidence_count: int
    valid: bool
    head_hash: str | None
    checked_at: datetime
    problems: list[ChainProblemOut]


class VerificationRequest(BaseModel):
    serial_number: str | None = Field(default=None, max_length=120)
    imei: str | None = Field(default=None, max_length=32)
    barcode: str | None = Field(default=None, max_length=64)
    batch_code: str | None = Field(default=None, max_length=64)
    seal_intact: bool | None = None
    accessories_present: bool | None = None
    tags_present: bool | None = None
    weight_grams: int | None = Field(default=None, ge=0, le=100_000)


class VerificationOutFull(ApiModel):
    id: int
    return_request_id: int
    status: str
    score: float
    reasons: list[object]
    checklist: dict[str, object]
    identifier_checks: dict[str, object]
    returned_identifiers: dict[str, object]
    image_distance: int | None
    dispatch_evidence_id: int | None
    return_evidence_id: int | None
    created_at: datetime
    updated_at: datetime


class CsvIssueOut(BaseModel):
    row_number: int
    message: str


class CsvVerificationResult(BaseModel):
    processed: int
    results: list[VerificationOutFull]
    errors: list[CsvIssueOut]


class ClaimCreate(BaseModel):
    template_code: str | None = None
    amount_claimed_paise: int | None = Field(default=None, gt=0)
    notes: str | None = Field(default=None, max_length=2000)


class ClaimResolve(BaseModel):
    status: str
    amount_approved_paise: int | None = Field(default=None, ge=0)


class ClaimOut(ApiModel):
    id: int
    reference: str
    status: str
    template_code: str
    order_id: int
    return_request_id: int | None
    marketplace_id: int
    amount_claimed_paise: int
    amount_approved_paise: int
    filed_at: datetime | None
    deadline_at: datetime | None
    resolved_at: datetime | None
    pack_path: str | None
    notes: str | None
    created_at: datetime
    updated_at: datetime


class ClaimTemplateOut(BaseModel):
    code: str
    name: str
    title: str
    subtitle: str
    sections: list[str]


class SettlementLineOut(ApiModel):
    id: int
    order_number: str
    transaction_type: str
    marketplace_sku: str | None
    quantity: int
    gross_paise: int
    commission_paise: int
    shipping_paise: int
    reverse_shipping_paise: int
    refund_paise: int
    other_fee_paise: int
    net_paise: int


class SettlementOut(ApiModel):
    id: int
    marketplace_id: int
    file_name: str
    file_sha256: str
    period_start: datetime | None
    period_end: datetime | None
    imported_at: datetime
    row_count: int
    total_net_paise: int
    status: str


class SettlementDetailOut(SettlementOut):
    lines: list[SettlementLineOut]


class SettlementImportOut(BaseModel):
    settlement: SettlementOut
    rows: int
    issues: list[str]
    entries_created: int
    entries_recovered: int
    detected_paise: int


class ReconciliationOut(BaseModel):
    settlement_id: int | None
    lines_checked: int
    entries_created: int
    entries_recovered: int
    detected_paise: int
    by_category: dict[str, int]


class RecoveryEntryOut(ApiModel):
    id: int
    claim_id: int | None
    order_id: int | None
    return_request_id: int | None
    marketplace_id: int | None
    direction: str
    status: str
    category: str
    amount_paise: int
    detected_at: datetime
    recovered_at: datetime | None
    description: str


class RecoverySummaryRowOut(BaseModel):
    marketplace_id: int | None
    marketplace: str
    month: str
    at_risk_paise: int
    recovered_paise: int
    open_entries: int
    recovered_entries: int


class RecoverySummaryOut(BaseModel):
    rows: list[RecoverySummaryRowOut]
    total_at_risk_paise: int
    total_recovered_paise: int


class ApiKeyCreate(BaseModel):
    name: str = Field(min_length=2, max_length=120)


class ApiKeyCreated(BaseModel):
    id: int
    name: str
    prefix: str
    token: str
    warning: str = "Store this token now; it is not shown again."
