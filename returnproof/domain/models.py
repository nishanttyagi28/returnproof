"""SQLAlchemy 2.0 domain models.

All money is stored as integer paise. All timestamps are UTC (displayed as IST
by the web layer). Every business record is scoped to an organisation so that
multi-tenant isolation can be enforced at the query boundary.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, ClassVar

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from returnproof.domain.enums import (
    ClaimStatus,
    EvidenceKind,
    LeakageCategory,
    MarketplaceCode,
    OrderStatus,
    RecoveryDirection,
    RecoveryStatus,
    ReturnStatus,
    Role,
    ShipmentStatus,
    VerificationStatus,
)

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def utcnow() -> datetime:
    """Timezone-aware current UTC time."""
    return datetime.now(UTC)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map: ClassVar[dict[Any, Any]] = {dict[str, Any]: JSON, list[Any]: JSON}


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Organisation(TimestampMixin, Base):
    __tablename__ = "organisations"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(160))
    legal_name: Mapped[str | None] = mapped_column(String(200))
    gstin: Mapped[str | None] = mapped_column(String(15), unique=True)
    email: Mapped[str] = mapped_column(String(200))
    phone: Mapped[str | None] = mapped_column(String(20))
    address_line1: Mapped[str | None] = mapped_column(String(200))
    address_line2: Mapped[str | None] = mapped_column(String(200))
    city: Mapped[str | None] = mapped_column(String(80))
    state: Mapped[str | None] = mapped_column(String(80))
    pincode: Mapped[str | None] = mapped_column(String(6))
    is_sample: Mapped[bool] = mapped_column(Boolean, default=False)

    users: Mapped[list[User]] = relationship(back_populates="organisation")
    orders: Mapped[list[Order]] = relationship(back_populates="organisation")


class User(TimestampMixin, Base):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("org_id", "email", name="uq_users_org_email"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(
        ForeignKey("organisations.id", ondelete="CASCADE"), index=True
    )
    email: Mapped[str] = mapped_column(String(200), index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    full_name: Mapped[str] = mapped_column(String(120))
    role: Mapped[str] = mapped_column(String(16), default=Role.OPS.value)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    organisation: Mapped[Organisation] = relationship(back_populates="users")


class ApiKey(TimestampMixin, Base):
    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(
        ForeignKey("organisations.id", ondelete="CASCADE"), index=True
    )
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    name: Mapped[str] = mapped_column(String(120))
    prefix: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    key_hash: Mapped[str] = mapped_column(String(128))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Marketplace(TimestampMixin, Base):
    __tablename__ = "marketplaces"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120))
    claim_window_days: Mapped[int] = mapped_column(Integer, default=15)
    supports_safe_t: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    @property
    def enum_code(self) -> MarketplaceCode | None:
        try:
            return MarketplaceCode(self.code)
        except ValueError:
            return None


class Product(TimestampMixin, Base):
    __tablename__ = "products"
    __table_args__ = (UniqueConstraint("org_id", "sku", name="uq_products_org_sku"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(
        ForeignKey("organisations.id", ondelete="CASCADE"), index=True
    )
    sku: Mapped[str] = mapped_column(String(64), index=True)
    title: Mapped[str] = mapped_column(String(240))
    category: Mapped[str | None] = mapped_column(String(80))
    brand: Mapped[str | None] = mapped_column(String(80))
    unit_price_paise: Mapped[int] = mapped_column(Integer, default=0)
    expected_weight_grams: Mapped[int | None] = mapped_column(Integer)
    requires_serial: Mapped[bool] = mapped_column(Boolean, default=False)
    is_sample: Mapped[bool] = mapped_column(Boolean, default=False)


class Order(TimestampMixin, Base):
    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint(
            "org_id", "marketplace_id", "order_number", name="uq_orders_tenant_number"
        ),
        Index("ix_orders_org_placed", "org_id", "placed_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(
        ForeignKey("organisations.id", ondelete="CASCADE"), index=True
    )
    marketplace_id: Mapped[int] = mapped_column(ForeignKey("marketplaces.id"), index=True)
    order_number: Mapped[str] = mapped_column(String(64), index=True)
    placed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    return_window_ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    customer_name: Mapped[str] = mapped_column(String(120))
    customer_pincode: Mapped[str | None] = mapped_column(String(6))
    customer_state: Mapped[str | None] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(32), default=OrderStatus.PENDING.value)
    total_paise: Mapped[int] = mapped_column(Integer, default=0)
    payment_method: Mapped[str] = mapped_column(String(32), default="prepaid")
    is_sample: Mapped[bool] = mapped_column(Boolean, default=False)

    organisation: Mapped[Organisation] = relationship(back_populates="orders")
    marketplace: Mapped[Marketplace] = relationship()
    items: Mapped[list[OrderItem]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )
    shipments: Mapped[list[Shipment]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )
    returns: Mapped[list[ReturnRequest]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )


class OrderItem(TimestampMixin, Base):
    __tablename__ = "order_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id", ondelete="SET NULL"))
    sku_snapshot: Mapped[str] = mapped_column(String(64))
    title_snapshot: Mapped[str] = mapped_column(String(240))
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    unit_price_paise: Mapped[int] = mapped_column(Integer, default=0)
    serial_number: Mapped[str | None] = mapped_column(String(120))
    imei: Mapped[str | None] = mapped_column(String(32))
    batch_code: Mapped[str | None] = mapped_column(String(64))
    expected_weight_grams: Mapped[int | None] = mapped_column(Integer)

    order: Mapped[Order] = relationship(back_populates="items")
    product: Mapped[Product | None] = relationship()


class Shipment(TimestampMixin, Base):
    __tablename__ = "shipments"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    awb: Mapped[str] = mapped_column(String(64), index=True)
    courier: Mapped[str] = mapped_column(String(80))
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    weight_grams: Mapped[int | None] = mapped_column(Integer)
    package_count: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(24), default=ShipmentStatus.CREATED.value)

    order: Mapped[Order] = relationship(back_populates="shipments")


class ReturnRequest(TimestampMixin, Base):
    __tablename__ = "return_requests"
    __table_args__ = (
        UniqueConstraint("order_id", "rma_number", name="uq_returns_order_rma"),
        Index("ix_returns_org_status", "org_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(
        ForeignKey("organisations.id", ondelete="CASCADE"), index=True
    )
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    rma_number: Mapped[str] = mapped_column(String(64), index=True)
    marketplace_return_id: Mapped[str | None] = mapped_column(String(80))
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reason_code: Mapped[str] = mapped_column(String(48), default="other")
    reason_text: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default=ReturnStatus.REQUESTED.value)
    refund_amount_paise: Mapped[int] = mapped_column(Integer, default=0)
    reverse_shipping_paise: Mapped[int] = mapped_column(Integer, default=0)
    buyer_comment: Mapped[str | None] = mapped_column(Text)
    is_sample: Mapped[bool] = mapped_column(Boolean, default=False)

    order: Mapped[Order] = relationship(back_populates="returns")
    evidence: Mapped[list[Evidence]] = relationship(
        back_populates="return_request", cascade="all, delete-orphan"
    )
    verification: Mapped[VerificationResult | None] = relationship(
        back_populates="return_request", uselist=False
    )


class Evidence(TimestampMixin, Base):
    __tablename__ = "evidence"
    __table_args__ = (
        UniqueConstraint("order_id", "chain_index", name="uq_evidence_order_chain_index"),
        Index("ix_evidence_org_stage", "org_id", "stage"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(
        ForeignKey("organisations.id", ondelete="CASCADE"), index=True
    )
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    return_request_id: Mapped[int | None] = mapped_column(
        ForeignKey("return_requests.id", ondelete="SET NULL"), index=True
    )
    stage: Mapped[str] = mapped_column(String(24))
    kind: Mapped[str] = mapped_column(String(16), default=EvidenceKind.PHOTO.value)
    chain_index: Mapped[int] = mapped_column(Integer)
    filename: Mapped[str] = mapped_column(String(255))
    stored_path: Mapped[str] = mapped_column(String(500))
    content_type: Mapped[str] = mapped_column(String(120))
    size_bytes: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    previous_hash: Mapped[str] = mapped_column(String(64))
    chain_hash: Mapped[str] = mapped_column(String(64), unique=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    uploaded_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    device_note: Mapped[str | None] = mapped_column(String(240))
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    perceptual_hash: Mapped[str | None] = mapped_column(String(64))
    is_sample: Mapped[bool] = mapped_column(Boolean, default=False)

    return_request: Mapped[ReturnRequest | None] = relationship(back_populates="evidence")


class VerificationResult(TimestampMixin, Base):
    __tablename__ = "verification_results"
    __table_args__ = (UniqueConstraint("return_request_id", name="uq_verification_return"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(
        ForeignKey("organisations.id", ondelete="CASCADE"), index=True
    )
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    return_request_id: Mapped[int] = mapped_column(
        ForeignKey("return_requests.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(String(24), default=VerificationStatus.NEEDS_REVIEW.value)
    score: Mapped[float] = mapped_column(Float, default=0.0)
    reasons: Mapped[list[Any]] = mapped_column(JSON, default=list)
    checklist: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    identifier_checks: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    image_distance: Mapped[int | None] = mapped_column(Integer)
    dispatch_evidence_id: Mapped[int | None] = mapped_column(ForeignKey("evidence.id"))
    return_evidence_id: Mapped[int | None] = mapped_column(ForeignKey("evidence.id"))
    verified_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))

    return_request: Mapped[ReturnRequest] = relationship(back_populates="verification")


class Claim(TimestampMixin, Base):
    __tablename__ = "claims"
    __table_args__ = (Index("ix_claims_org_status", "org_id", "status"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(
        ForeignKey("organisations.id", ondelete="CASCADE"), index=True
    )
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    return_request_id: Mapped[int | None] = mapped_column(
        ForeignKey("return_requests.id", ondelete="SET NULL"), index=True
    )
    marketplace_id: Mapped[int] = mapped_column(ForeignKey("marketplaces.id"), index=True)
    reference: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(16), default=ClaimStatus.DRAFT.value)
    template_code: Mapped[str] = mapped_column(String(48), default="generic")
    amount_claimed_paise: Mapped[int] = mapped_column(Integer, default=0)
    amount_approved_paise: Mapped[int] = mapped_column(Integer, default=0)
    filed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)
    pack_path: Mapped[str | None] = mapped_column(String(500))
    is_sample: Mapped[bool] = mapped_column(Boolean, default=False)


class Settlement(TimestampMixin, Base):
    __tablename__ = "settlements"

    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(
        ForeignKey("organisations.id", ondelete="CASCADE"), index=True
    )
    marketplace_id: Mapped[int] = mapped_column(ForeignKey("marketplaces.id"), index=True)
    period_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    file_name: Mapped[str] = mapped_column(String(255))
    file_sha256: Mapped[str] = mapped_column(String(64))
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    imported_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    total_net_paise: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(24), default="imported")
    is_sample: Mapped[bool] = mapped_column(Boolean, default=False)

    lines: Mapped[list[SettlementLine]] = relationship(
        back_populates="settlement", cascade="all, delete-orphan"
    )


class SettlementLine(TimestampMixin, Base):
    __tablename__ = "settlement_lines"

    id: Mapped[int] = mapped_column(primary_key=True)
    settlement_id: Mapped[int] = mapped_column(
        ForeignKey("settlements.id", ondelete="CASCADE"), index=True
    )
    order_number: Mapped[str] = mapped_column(String(64), index=True)
    transaction_type: Mapped[str] = mapped_column(String(48))
    marketplace_sku: Mapped[str | None] = mapped_column(String(64))
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    gross_paise: Mapped[int] = mapped_column(Integer, default=0)
    commission_paise: Mapped[int] = mapped_column(Integer, default=0)
    shipping_paise: Mapped[int] = mapped_column(Integer, default=0)
    reverse_shipping_paise: Mapped[int] = mapped_column(Integer, default=0)
    refund_paise: Mapped[int] = mapped_column(Integer, default=0)
    other_fee_paise: Mapped[int] = mapped_column(Integer, default=0)
    net_paise: Mapped[int] = mapped_column(Integer, default=0)
    raw: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    settlement: Mapped[Settlement] = relationship(back_populates="lines")


class RecoveryEntry(TimestampMixin, Base):
    __tablename__ = "recovery_entries"
    __table_args__ = (Index("ix_recovery_org_direction", "org_id", "direction"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(
        ForeignKey("organisations.id", ondelete="CASCADE"), index=True
    )
    claim_id: Mapped[int | None] = mapped_column(
        ForeignKey("claims.id", ondelete="SET NULL"), index=True
    )
    order_id: Mapped[int | None] = mapped_column(
        ForeignKey("orders.id", ondelete="SET NULL"), index=True
    )
    return_request_id: Mapped[int | None] = mapped_column(
        ForeignKey("return_requests.id", ondelete="SET NULL")
    )
    settlement_line_id: Mapped[int | None] = mapped_column(
        ForeignKey("settlement_lines.id", ondelete="SET NULL")
    )
    marketplace_id: Mapped[int | None] = mapped_column(ForeignKey("marketplaces.id"), index=True)
    direction: Mapped[str] = mapped_column(String(16), default=RecoveryDirection.AT_RISK.value)
    status: Mapped[str] = mapped_column(String(16), default=RecoveryStatus.OPEN.value)
    category: Mapped[str] = mapped_column(String(48), default=LeakageCategory.OTHER.value)
    amount_paise: Mapped[int] = mapped_column(Integer)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    recovered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    description: Mapped[str] = mapped_column(String(400))
    is_sample: Mapped[bool] = mapped_column(Boolean, default=False)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (Index("ix_audit_org_created", "org_id", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(
        ForeignKey("organisations.id", ondelete="CASCADE"), index=True
    )
    actor_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    entity_type: Mapped[str] = mapped_column(String(48))
    entity_id: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64))
    summary: Mapped[str] = mapped_column(String(400))
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    ip_address: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
