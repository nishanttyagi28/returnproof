"""Organisation-scoped repository queries.

Every function here takes ``org_id`` explicitly. Route modules must use these
helpers rather than issuing unscoped queries, which keeps tenant isolation in a
single auditable place.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session, selectinload

from returnproof.domain.enums import ClaimStatus, RecoveryDirection, RecoveryStatus
from returnproof.domain.models import (
    Claim,
    Evidence,
    Marketplace,
    Order,
    Organisation,
    RecoveryEntry,
    ReturnRequest,
    Shipment,
    User,
    VerificationResult,
)


def scoped_orders(org_id: int) -> Select[Any]:
    return select(Order).where(Order.org_id == org_id).order_by(Order.placed_at.desc())


def list_orders(
    db: Session,
    org_id: int,
    *,
    status: str | None = None,
    marketplace_id: int | None = None,
    search: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[Order]:
    stmt = scoped_orders(org_id).options(
        selectinload(Order.marketplace),
        selectinload(Order.items),
        selectinload(Order.returns),
    )
    if status:
        stmt = stmt.where(Order.status == status)
    if marketplace_id:
        stmt = stmt.where(Order.marketplace_id == marketplace_id)
    if search:
        like = f"%{search.strip()}%"
        stmt = stmt.where(Order.order_number.ilike(like) | Order.customer_name.ilike(like))
    return list(db.scalars(stmt.limit(limit).offset(offset)).unique().all())


def count_orders(db: Session, org_id: int) -> int:
    return int(db.scalar(select(func.count(Order.id)).where(Order.org_id == org_id)) or 0)


def get_order(db: Session, org_id: int, order_id: int) -> Order | None:
    return db.scalar(
        select(Order)
        .where(Order.id == order_id, Order.org_id == org_id)
        .options(
            selectinload(Order.marketplace),
            selectinload(Order.items),
            selectinload(Order.shipments),
            selectinload(Order.returns).selectinload(ReturnRequest.evidence),
            selectinload(Order.returns).selectinload(ReturnRequest.verification),
        )
    )


def get_order_by_number(db: Session, org_id: int, order_number: str) -> Order | None:
    return db.scalar(
        select(Order).where(Order.org_id == org_id, Order.order_number == order_number)
    )


def list_returns(
    db: Session,
    org_id: int,
    *,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[ReturnRequest]:
    stmt = (
        select(ReturnRequest)
        .where(ReturnRequest.org_id == org_id)
        .options(
            selectinload(ReturnRequest.order).selectinload(Order.marketplace),
            selectinload(ReturnRequest.verification),
        )
        .order_by(ReturnRequest.requested_at.desc())
    )
    if status:
        stmt = stmt.where(ReturnRequest.status == status)
    return list(db.scalars(stmt.limit(limit).offset(offset)).unique().all())


def get_return(db: Session, org_id: int, return_id: int) -> ReturnRequest | None:
    return db.scalar(
        select(ReturnRequest)
        .where(ReturnRequest.id == return_id, ReturnRequest.org_id == org_id)
        .options(
            selectinload(ReturnRequest.order).selectinload(Order.marketplace),
            selectinload(ReturnRequest.order).selectinload(Order.items),
            selectinload(ReturnRequest.evidence),
            selectinload(ReturnRequest.verification),
        )
    )


def list_evidence(db: Session, org_id: int, order_id: int) -> list[Evidence]:
    return list(
        db.scalars(
            select(Evidence)
            .where(Evidence.org_id == org_id, Evidence.order_id == order_id)
            .order_by(Evidence.chain_index)
        ).all()
    )


def list_claims(db: Session, org_id: int, *, status: str | None = None) -> list[Claim]:
    stmt = select(Claim).where(Claim.org_id == org_id)
    if status:
        stmt = stmt.where(Claim.status == status)
    return list(db.scalars(stmt.order_by(Claim.created_at.desc())).all())


def list_recoveries(
    db: Session,
    org_id: int,
    *,
    direction: str | None = None,
    limit: int = 100,
) -> list[RecoveryEntry]:
    stmt = (
        select(RecoveryEntry)
        .where(RecoveryEntry.org_id == org_id)
        .order_by(RecoveryEntry.detected_at.desc())
    )
    if direction:
        stmt = stmt.where(RecoveryEntry.direction == direction)
    return list(db.scalars(stmt.limit(limit)).all())


def list_marketplaces(db: Session) -> list[Marketplace]:
    return list(db.scalars(select(Marketplace).order_by(Marketplace.name)).all())


def list_users(db: Session, org_id: int) -> list[User]:
    return list(db.scalars(select(User).where(User.org_id == org_id).order_by(User.id)).all())


def get_organisation(db: Session, org_id: int) -> Organisation | None:
    return db.get(Organisation, org_id)


def list_audit_events(
    db: Session, org_id: int, *, entity_type: str | None = None, limit: int = 100
) -> list[object]:
    from returnproof.domain.models import AuditEvent

    stmt = (
        select(AuditEvent)
        .where(AuditEvent.org_id == org_id)
        .order_by(AuditEvent.created_at.desc())
    )
    if entity_type:
        stmt = stmt.where(AuditEvent.entity_type == entity_type)
    return list(db.scalars(stmt.limit(limit)).all())


@dataclass
class DashboardMetrics:
    returns_this_week: int
    mismatch_count: int
    verification_count: int
    money_at_risk_paise: int
    money_recovered_paise: int
    open_claims: int
    claims_due_soon: int

    @property
    def mismatch_rate(self) -> float:
        if self.verification_count == 0:
            return 0.0
        return self.mismatch_count / self.verification_count


def dashboard_metrics(db: Session, org_id: int) -> DashboardMetrics:
    now = datetime.now(UTC)
    week_ago = now - timedelta(days=7)
    returns_this_week = int(
        db.scalar(
            select(func.count(ReturnRequest.id)).where(
                ReturnRequest.org_id == org_id, ReturnRequest.requested_at >= week_ago
            )
        )
        or 0
    )
    verification_count = int(
        db.scalar(
            select(func.count(VerificationResult.id)).where(VerificationResult.org_id == org_id)
        )
        or 0
    )
    mismatch_count = int(
        db.scalar(
            select(func.count(VerificationResult.id)).where(
                VerificationResult.org_id == org_id,
                VerificationResult.status.in_(["mismatch", "needs_review"]),
            )
        )
        or 0
    )
    risk = int(
        db.scalar(
            select(func.coalesce(func.sum(RecoveryEntry.amount_paise), 0)).where(
                RecoveryEntry.org_id == org_id,
                RecoveryEntry.direction == RecoveryDirection.AT_RISK.value,
                RecoveryEntry.status == RecoveryStatus.OPEN.value,
            )
        )
        or 0
    )
    recovered = int(
        db.scalar(
            select(func.coalesce(func.sum(RecoveryEntry.amount_paise), 0)).where(
                RecoveryEntry.org_id == org_id,
                RecoveryEntry.direction == RecoveryDirection.RECOVERED.value,
            )
        )
        or 0
    )
    open_claims = int(
        db.scalar(
            select(func.count(Claim.id)).where(
                Claim.org_id == org_id, Claim.status.in_([ClaimStatus.DRAFT.value, ClaimStatus.FILED.value])
            )
        )
        or 0
    )
    due_soon = int(
        db.scalar(
            select(func.count(Claim.id)).where(
                Claim.org_id == org_id,
                Claim.status == ClaimStatus.FILED.value,
                Claim.deadline_at.is_not(None),
                Claim.deadline_at <= now + timedelta(days=7),
            )
        )
        or 0
    )
    return DashboardMetrics(
        returns_this_week=returns_this_week,
        mismatch_count=mismatch_count,
        verification_count=verification_count,
        money_at_risk_paise=risk,
        money_recovered_paise=recovered,
        open_claims=open_claims,
        claims_due_soon=due_soon,
    )


def shipment_for_order(db: Session, order_id: int) -> Shipment | None:
    return db.scalar(select(Shipment).where(Shipment.order_id == order_id))
