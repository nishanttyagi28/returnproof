"""CSV exports with formula-injection safeguards."""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from returnproof.domain.models import (
    AuditEvent,
    Claim,
    Order,
    RecoveryEntry,
    ReturnRequest,
    User,
)

DATASETS = ("orders", "returns", "claims", "recovery", "audit")


class ExportError(ValueError):
    pass


def _safe(value: Any) -> Any:
    if isinstance(value, str) and value and value[0] in {"=", "+", "-", "@", "\t", "\r"}:
        return "'" + value
    return value


def _iso(value: datetime | None) -> str:
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _csv(headers: list[str], rows: list[list[Any]]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(headers)
    for row in rows:
        writer.writerow([_safe(cell) for cell in row])
    return buffer.getvalue()


def export_orders(db: Session, org_id: int) -> tuple[str, str]:
    orders = list(
        db.scalars(
            select(Order)
            .where(Order.org_id == org_id)
            .options(selectinload(Order.marketplace), selectinload(Order.returns))
            .order_by(Order.placed_at.desc())
        ).all()
    )
    rows = [
        [
            order.order_number,
            order.marketplace.name,
            order.status,
            order.customer_name,
            order.customer_pincode or "",
            _iso(order.placed_at),
            _iso(order.dispatched_at),
            _iso(order.delivered_at),
            order.total_paise,
            "INR",
            order.payment_method,
            len(order.returns),
        ]
        for order in orders
    ]
    return (
        "orders",
        _csv(
            [
                "order_number",
                "marketplace",
                "status",
                "customer_name",
                "customer_pincode",
                "placed_at_utc",
                "dispatched_at_utc",
                "delivered_at_utc",
                "total_paise",
                "currency",
                "payment_method",
                "return_count",
            ],
            rows,
        ),
    )


def export_returns(db: Session, org_id: int) -> tuple[str, str]:
    returns = list(
        db.scalars(
            select(ReturnRequest)
            .where(ReturnRequest.org_id == org_id)
            .options(
                selectinload(ReturnRequest.order).selectinload(Order.marketplace),
                selectinload(ReturnRequest.verification),
            )
            .order_by(ReturnRequest.requested_at.desc())
        ).all()
    )
    rows = [
        [
            record.rma_number,
            record.order.order_number,
            record.order.marketplace.name,
            record.status,
            record.reason_code,
            record.reason_text or "",
            _iso(record.requested_at),
            _iso(record.received_at),
            record.refund_amount_paise,
            record.reverse_shipping_paise,
            record.verification.status if record.verification else "pending",
            round(record.verification.score, 4) if record.verification else "",
        ]
        for record in returns
    ]
    return (
        "returns",
        _csv(
            [
                "rma_number",
                "order_number",
                "marketplace",
                "status",
                "reason_code",
                "reason_text",
                "requested_at_utc",
                "received_at_utc",
                "refund_paise",
                "reverse_shipping_paise",
                "verification_status",
                "verification_score",
            ],
            rows,
        ),
    )


def export_claims(db: Session, org_id: int) -> tuple[str, str]:
    claims = list(
        db.scalars(
            select(Claim)
            .where(Claim.org_id == org_id)
            .options(selectinload(Claim.marketplace), selectinload(Claim.return_request))
            .order_by(Claim.created_at.desc())
        ).all()
    )
    rows = [
        [
            claim.reference,
            claim.order_id,
            claim.return_request.rma_number if claim.return_request else "",
            claim.marketplace.name,
            claim.status,
            claim.template_code,
            claim.amount_claimed_paise,
            claim.amount_approved_paise,
            _iso(claim.filed_at),
            _iso(claim.deadline_at),
            _iso(claim.resolved_at),
        ]
        for claim in claims
    ]
    return (
        "claims",
        _csv(
            [
                "reference",
                "order_id",
                "rma_number",
                "marketplace",
                "status",
                "template_code",
                "amount_claimed_paise",
                "amount_approved_paise",
                "filed_at_utc",
                "deadline_at_utc",
                "resolved_at_utc",
            ],
            rows,
        ),
    )


def export_recovery(db: Session, org_id: int) -> tuple[str, str]:
    entries = list(
        db.scalars(
            select(RecoveryEntry)
            .where(RecoveryEntry.org_id == org_id)
            .order_by(RecoveryEntry.detected_at.desc())
        ).all()
    )
    orders = {
        order.id: order.order_number
        for order in db.scalars(select(Order).where(Order.org_id == org_id)).all()
    }
    claims = {
        claim.id: claim.reference
        for claim in db.scalars(select(Claim).where(Claim.org_id == org_id)).all()
    }
    rows = [
        [
            _iso(entry.detected_at),
            entry.category,
            entry.direction,
            entry.status,
            entry.amount_paise,
            orders.get(entry.order_id or 0, ""),
            claims.get(entry.claim_id or 0, ""),
            entry.description,
            _iso(entry.recovered_at),
        ]
        for entry in entries
    ]
    return (
        "recovery",
        _csv(
            [
                "detected_at_utc",
                "category",
                "direction",
                "status",
                "amount_paise",
                "order_number",
                "claim_reference",
                "description",
                "recovered_at_utc",
            ],
            rows,
        ),
    )


def export_audit(db: Session, org_id: int) -> tuple[str, str]:
    events = list(
        db.scalars(
            select(AuditEvent)
            .where(AuditEvent.org_id == org_id)
            .order_by(AuditEvent.created_at.desc())
        ).all()
    )
    users = {
        user.id: user.email
        for user in db.scalars(select(User).where(User.org_id == org_id)).all()
    }
    rows = [
        [
            _iso(event.created_at),
            users.get(event.actor_user_id or 0, "system"),
            event.entity_type,
            event.entity_id,
            event.action,
            event.summary,
            event.ip_address or "",
        ]
        for event in events
    ]
    return (
        "audit",
        _csv(
            [
                "created_at_utc",
                "actor",
                "entity_type",
                "entity_id",
                "action",
                "summary",
                "ip_address",
            ],
            rows,
        ),
    )


EXPORTERS = {
    "orders": export_orders,
    "returns": export_returns,
    "claims": export_claims,
    "recovery": export_recovery,
    "audit": export_audit,
}


def build_export(db: Session, org_id: int, dataset: str) -> tuple[str, str]:
    exporter = EXPORTERS.get(dataset)
    if exporter is None:
        raise ExportError(f"Unknown dataset {dataset!r}")
    return exporter(db, org_id)
