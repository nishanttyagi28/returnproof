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


class ApiKeyCreate(BaseModel):
    name: str = Field(min_length=2, max_length=120)


class ApiKeyCreated(BaseModel):
    id: int
    name: str
    prefix: str
    token: str
    warning: str = "Store this token now; it is not shown again."
