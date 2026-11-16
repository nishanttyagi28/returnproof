"""Settlement CSV import, reconciliation and leakage detection."""

from __future__ import annotations

import csv
import hashlib
import io
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from returnproof.config import Settings
from returnproof.domain.enums import (
    ClaimStatus,
    LeakageCategory,
    RecoveryDirection,
    RecoveryStatus,
    ReturnStatus,
)
from returnproof.domain.models import (
    Claim,
    Marketplace,
    Order,
    RecoveryEntry,
    ReturnRequest,
    Settlement,
    SettlementLine,
)

SELLER_PROTECTED_REASONS = {
    "damaged",
    "empty_box",
    "wrong_item",
    "not_as_described",
    "missing_accessories",
}
RECEIVED_STATUSES = {
    ReturnStatus.RECEIVED.value,
    ReturnStatus.VERIFIED.value,
    ReturnStatus.CLOSED.value,
}
CLAIM_PAID_STATUSES = {
    ClaimStatus.APPROVED.value,
    ClaimStatus.PARTIAL.value,
}

FIELD_ALIASES: dict[str, set[str]] = {
    "order_number": {"order_number", "order_id", "order", "orderid", "amazon_order_id"},
    "transaction_type": {"transaction_type", "type", "transaction", "event_type"},
    "marketplace_sku": {"marketplace_sku", "sku", "seller_sku"},
    "quantity": {"quantity", "qty", "units"},
    "gross": {"gross_inr", "gross", "product_sales", "sales", "item_price", "gross_amount"},
    "commission": {"commission_inr", "commission", "referral_fee", "marketplace_fee"},
    "shipping": {"shipping_inr", "shipping", "shipping_fee", "delivery_fee"},
    "reverse_shipping": {
        "reverse_shipping_inr",
        "reverse_shipping",
        "reverse_shipping_fee",
        "return_shipping",
    },
    "refund": {"refund_inr", "refund", "refund_amount", "customer_refund"},
    "other_fee": {"other_fee_inr", "other_fee", "other_fees", "other_fee_amount"},
    "net": {"net_inr", "net", "net_amount", "settlement_amount", "payout"},
    "claim_payout": {
        "claim_payout_inr",
        "claim_payout",
        "reimbursement",
        "reimbursement_amount",
    },
}


class SettlementError(ValueError):
    """Invalid settlement input."""


class SettlementConflict(SettlementError):
    """The same settlement file was already imported."""


@dataclass
class SettlementRow:
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
    claim_payout_paise: int
    raw: dict[str, Any]


@dataclass
class ImportResult:
    settlement: Settlement
    rows: int
    issues: list[str]
    entries_created: int = 0
    entries_recovered: int = 0
    detected_paise: int = 0


@dataclass
class ReconciliationReport:
    settlement_id: int | None
    lines_checked: int
    entries_created: int = 0
    entries_recovered: int = 0
    detected_paise: int = 0
    by_category: dict[str, int] = field(default_factory=dict)


@dataclass
class RecoverySummaryRow:
    marketplace_id: int | None
    marketplace: str
    month: str
    at_risk_paise: int
    recovered_paise: int
    open_entries: int
    recovered_entries: int


@dataclass
class RecoverySummary:
    rows: list[RecoverySummaryRow]
    total_at_risk_paise: int
    total_recovered_paise: int


def _normalize_header(value: str) -> str:
    cleaned = value.strip().lower().lstrip("\ufeff").replace("&", " and ")
    return "_".join(part for part in "".join(ch if ch.isalnum() else "_" for ch in cleaned).split("_") if part)


def _header_map(fieldnames: Sequence[str]) -> dict[str, str]:
    normalized = {name: _normalize_header(name) for name in fieldnames}
    mapping: dict[str, str] = {}
    for canonical, aliases in FIELD_ALIASES.items():
        for original, normalized_name in normalized.items():
            if normalized_name in aliases:
                mapping[canonical] = original
                break
    return mapping


def parse_money(value: str | None) -> int:
    """Parse INR text (commas, currency symbols, parentheses) into integer paise."""
    if value is None:
        return 0
    text = str(value).strip()
    if not text:
        return 0
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()").replace(",", "").replace(" ", "")
    for symbol in ("₹", "INR", "Rs.", "Rs", "rupees"):
        text = text.replace(symbol, "")
    try:
        amount = Decimal(text)
    except InvalidOperation as exc:
        raise SettlementError(f"Cannot parse amount {value!r}") from exc
    if negative:
        amount = -amount
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _normalize_transaction(value: str) -> str:
    normalized = _normalize_header(value)
    if normalized in {"sale", "order", "order_payment", "payment", "sales"}:
        return "sale"
    if normalized in {"refund", "customer_refund"}:
        return "refund"
    if normalized in {"claim", "claim_payout", "reimbursement", "safe_t", "payout"}:
        return "claim_payout"
    if normalized in {"return", "return_fee"}:
        return "return"
    return normalized or "other"


def parse_settlement_csv(text: str) -> tuple[list[SettlementRow], list[str]]:
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    if not reader.fieldnames:
        raise SettlementError("CSV has no header row")
    headers = _header_map(reader.fieldnames)
    if "order_number" not in headers:
        raise SettlementError("CSV needs an order number column")
    if "transaction_type" not in headers:
        raise SettlementError("CSV needs a transaction type column")
    if not any(
        field in headers
        for field in (
            "gross",
            "commission",
            "shipping",
            "reverse_shipping",
            "refund",
            "other_fee",
            "net",
            "claim_payout",
        )
    ):
        raise SettlementError("CSV needs at least one monetary column")

    def raw(row: dict[str, str | None], key: str) -> str:
        header = headers.get(key)
        return (row.get(header) or "").strip() if header else ""

    rows: list[SettlementRow] = []
    issues: list[str] = []
    for line_number, row in enumerate(reader, start=2):
        order_number = raw(row, "order_number")
        if not order_number:
            issues.append(f"Row {line_number}: missing order number")
            continue
        try:
            quantity_text = raw(row, "quantity") or "1"
            try:
                quantity = int(float(quantity_text))
            except ValueError:
                quantity = 1
            gross = parse_money(raw(row, "gross"))
            commission = parse_money(raw(row, "commission"))
            shipping = parse_money(raw(row, "shipping"))
            reverse = parse_money(raw(row, "reverse_shipping"))
            refund = parse_money(raw(row, "refund"))
            other = parse_money(raw(row, "other_fee"))
            net_raw = raw(row, "net")
            net = parse_money(net_raw) if net_raw else gross - commission - shipping - reverse - refund - other
            claim_payout = parse_money(raw(row, "claim_payout"))
            transaction = _normalize_transaction(raw(row, "transaction_type"))
            if transaction == "claim_payout" and claim_payout == 0:
                claim_payout = gross or abs(net)
            rows.append(
                SettlementRow(
                    order_number=order_number,
                    transaction_type=transaction,
                    marketplace_sku=raw(row, "marketplace_sku") or None,
                    quantity=quantity,
                    gross_paise=gross,
                    commission_paise=commission,
                    shipping_paise=shipping,
                    reverse_shipping_paise=reverse,
                    refund_paise=refund,
                    other_fee_paise=other,
                    net_paise=net,
                    claim_payout_paise=claim_payout,
                    raw={key: value for key, value in row.items() if key is not None},
                )
            )
        except SettlementError as exc:
            issues.append(f"Row {line_number}: {exc}")
    if not rows:
        raise SettlementError("CSV contained no usable settlement rows")
    return rows, issues


def _settlement_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def import_settlement(
    db: Session,
    settings: Settings,
    *,
    org_id: int,
    marketplace: Marketplace,
    file_name: str,
    content: bytes,
    imported_by_id: int | None,
    period_start: datetime | None = None,
    period_end: datetime | None = None,
    reconcile: bool = True,
) -> ImportResult:
    digest = _settlement_hash(content)
    duplicate = db.scalar(
        select(Settlement).where(Settlement.org_id == org_id, Settlement.file_sha256 == digest)
    )
    if duplicate is not None:
        raise SettlementConflict(
            f"This file was already imported as settlement #{duplicate.id}"
        )
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise SettlementError("Settlement CSV must be UTF-8") from exc
    rows, issues = parse_settlement_csv(text)
    settlement = Settlement(
        org_id=org_id,
        marketplace_id=marketplace.id,
        period_start=period_start,
        period_end=period_end,
        file_name=file_name[:255],
        file_sha256=digest,
        imported_by_id=imported_by_id,
        row_count=len(rows),
        total_net_paise=sum(row.net_paise for row in rows),
        status="imported",
        is_sample=False,
    )
    db.add(settlement)
    db.flush()
    for row in rows:
        db.add(
            SettlementLine(
                settlement_id=settlement.id,
                order_number=row.order_number,
                transaction_type=row.transaction_type,
                marketplace_sku=row.marketplace_sku,
                quantity=row.quantity,
                gross_paise=row.gross_paise,
                commission_paise=row.commission_paise,
                shipping_paise=row.shipping_paise,
                reverse_shipping_paise=row.reverse_shipping_paise,
                refund_paise=row.refund_paise,
                other_fee_paise=row.other_fee_paise,
                net_paise=row.net_paise,
                raw={**row.raw, "claim_payout_paise": row.claim_payout_paise},
            )
        )
    db.commit()
    db.refresh(settlement)
    result = ImportResult(settlement=settlement, rows=len(rows), issues=issues)
    if reconcile:
        report = reconcile_settlement(db, settlement, settings=settings)
        result.entries_created = report.entries_created
        result.entries_recovered = report.entries_recovered
        result.detected_paise = report.detected_paise
    return result


def _received(record: ReturnRequest | None) -> bool:
    return bool(
        record is not None
        and record.received_at is not None
        and record.status in RECEIVED_STATUSES
    )


def _latest_return(db: Session, org_id: int, order_id: int) -> ReturnRequest | None:
    return db.scalar(
        select(ReturnRequest)
        .where(ReturnRequest.org_id == org_id, ReturnRequest.order_id == order_id)
        .order_by(ReturnRequest.id.desc())
    )


def _ensure_line_entry(
    db: Session,
    *,
    settlement: Settlement,
    line: SettlementLine,
    category: LeakageCategory,
    amount: int,
    description: str,
    order_id: int | None,
    return_id: int | None,
    marketplace_id: int,
) -> RecoveryEntry | None:
    if amount <= 0:
        return None
    existing = db.scalar(
        select(RecoveryEntry).where(
            RecoveryEntry.org_id == settlement.org_id,
            RecoveryEntry.settlement_line_id == line.id,
            RecoveryEntry.category == category.value,
        )
    )
    if existing is not None:
        return None
    entry = RecoveryEntry(
        org_id=settlement.org_id,
        order_id=order_id,
        return_request_id=return_id,
        settlement_line_id=line.id,
        marketplace_id=marketplace_id,
        direction=RecoveryDirection.AT_RISK.value,
        status=RecoveryStatus.OPEN.value,
        category=category.value,
        amount_paise=amount,
        description=description[:400],
        is_sample=settlement.is_sample,
    )
    db.add(entry)
    return entry


def reconcile_settlement(
    db: Session,
    settlement: Settlement,
    *,
    settings: Settings | None = None,
) -> ReconciliationReport:
    tolerance = settings.settlement_tolerance_paise if settings else 100
    default_commission = settings.default_commission_percent if settings else 12.0
    marketplace = db.get(Marketplace, settlement.marketplace_id)
    if marketplace is None:
        raise SettlementError("Settlement marketplace is missing")
    config = marketplace.config or {}
    try:
        commission_percent = float(config.get("commission_percent", default_commission))
    except (TypeError, ValueError):
        commission_percent = default_commission

    lines = list(
        db.scalars(
            select(SettlementLine)
            .where(SettlementLine.settlement_id == settlement.id)
            .order_by(SettlementLine.id)
        ).all()
    )
    report = ReconciliationReport(settlement_id=settlement.id, lines_checked=len(lines))
    orders = {
        order.order_number: order
        for order in db.scalars(select(Order).where(Order.org_id == settlement.org_id)).all()
    }

    def note(entry: RecoveryEntry | None, category: LeakageCategory) -> None:
        if entry is None:
            return
        report.entries_created += 1
        report.detected_paise += entry.amount_paise
        report.by_category[category.value] = (
            report.by_category.get(category.value, 0) + entry.amount_paise
        )

    for line in lines:
        order = orders.get(line.order_number)
        record = _latest_return(db, settlement.org_id, order.id) if order is not None else None
        received = _received(record)

        if (line.refund_paise > 0 or line.transaction_type == "refund") and not received:
            refund = line.refund_paise
            if refund == 0 and line.gross_paise > 0:
                refund = line.gross_paise
            detail = (
                "Refund issued with no return request on record."
                if record is None
                else "Refund issued while the return has not been received."
            )
            note(
                _ensure_line_entry(
                    db,
                    settlement=settlement,
                    line=line,
                    category=LeakageCategory.REFUND_WITHOUT_RETURN,
                    amount=refund,
                    description=detail,
                    order_id=order.id if order else None,
                    return_id=record.id if record else None,
                    marketplace_id=settlement.marketplace_id,
                ),
                LeakageCategory.REFUND_WITHOUT_RETURN,
            )

        if line.reverse_shipping_paise > 0:
            wrong = (
                record is None
                or not received
                or record.reason_code in SELLER_PROTECTED_REASONS
            )
            if wrong:
                note(
                    _ensure_line_entry(
                        db,
                        settlement=settlement,
                        line=line,
                        category=LeakageCategory.REVERSE_SHIPPING_ERROR,
                        amount=line.reverse_shipping_paise,
                        description=(
                            "Reverse shipping charged on a return that is not eligible "
                            "or was not received."
                        ),
                        order_id=order.id if order else None,
                        return_id=record.id if record else None,
                        marketplace_id=settlement.marketplace_id,
                    ),
                    LeakageCategory.REVERSE_SHIPPING_ERROR,
                )

        if line.gross_paise > 0 and line.transaction_type == "sale":
            expected_commission = int(
                Decimal(line.gross_paise)
                * Decimal(str(commission_percent))
                / Decimal(100)
            )
            excess = line.commission_paise - expected_commission
            if excess > tolerance:
                note(
                    _ensure_line_entry(
                        db,
                        settlement=settlement,
                        line=line,
                        category=LeakageCategory.COMMISSION_MISMATCH,
                        amount=excess,
                        description=(
                            f"Commission {line.commission_paise} paise exceeds the configured "
                            f"{commission_percent:.2f}% rate by {excess} paise."
                        ),
                        order_id=order.id if order else None,
                        return_id=record.id if record else None,
                        marketplace_id=settlement.marketplace_id,
                    ),
                    LeakageCategory.COMMISSION_MISMATCH,
                )

        expected_net = (
            line.gross_paise
            - line.commission_paise
            - line.shipping_paise
            - line.reverse_shipping_paise
            - line.refund_paise
            - line.other_fee_paise
        )
        if line.net_paise < expected_net - tolerance:
            shortfall = expected_net - line.net_paise
            note(
                _ensure_line_entry(
                    db,
                    settlement=settlement,
                    line=line,
                    category=LeakageCategory.FEE_MISMATCH,
                    amount=shortfall,
                    description=(
                        "Settlement net is lower than the amount implied by the line's "
                        "fees and refunds."
                    ),
                    order_id=order.id if order else None,
                    return_id=record.id if record else None,
                    marketplace_id=settlement.marketplace_id,
                ),
                LeakageCategory.FEE_MISMATCH,
            )

    db.commit()
    claim_report = reconcile_claims(db, settlement.org_id, now=datetime.now(UTC))
    report.entries_created += claim_report.entries_created
    report.entries_recovered += claim_report.entries_recovered
    report.detected_paise += claim_report.detected_paise
    for category, amount in claim_report.by_category.items():
        report.by_category[category] = report.by_category.get(category, 0) + amount
    settlement.status = "reconciled"
    db.commit()
    return report


def _payout_for_order(db: Session, org_id: int, order_number: str) -> int:
    total = db.scalar(
        select(func.coalesce(func.sum(SettlementLine.gross_paise), 0))
        .join(Settlement, Settlement.id == SettlementLine.settlement_id)
        .where(
            Settlement.org_id == org_id,
            SettlementLine.order_number == order_number,
            SettlementLine.transaction_type == "claim_payout",
        )
    )
    return int(total or 0)


def reconcile_claims(
    db: Session, org_id: int, *, now: datetime | None = None
) -> ReconciliationReport:
    timestamp = now or datetime.now(UTC)
    claims = list(
        db.scalars(
            select(Claim).where(
                Claim.org_id == org_id,
                Claim.status.in_(sorted(CLAIM_PAID_STATUSES)),
                Claim.amount_approved_paise > 0,
            )
        ).all()
    )
    report = ReconciliationReport(settlement_id=None, lines_checked=len(claims))
    for claim in claims:
        order = db.get(Order, claim.order_id)
        if order is None:
            continue
        paid = _payout_for_order(db, org_id, order.order_number)
        existing = db.scalar(
            select(RecoveryEntry)
            .where(
                RecoveryEntry.org_id == org_id,
                RecoveryEntry.claim_id == claim.id,
                RecoveryEntry.category == LeakageCategory.APPROVED_NOT_PAID.value,
            )
            .order_by(RecoveryEntry.id.desc())
        )
        if paid >= claim.amount_approved_paise:
            if existing is not None and existing.status != RecoveryStatus.RECOVERED.value:
                existing.direction = RecoveryDirection.RECOVERED.value
                existing.status = RecoveryStatus.RECOVERED.value
                existing.recovered_at = timestamp
                existing.amount_paise = claim.amount_approved_paise
                report.entries_recovered += 1
        elif existing is None:
            entry = RecoveryEntry(
                org_id=org_id,
                claim_id=claim.id,
                order_id=claim.order_id,
                return_request_id=claim.return_request_id,
                marketplace_id=claim.marketplace_id,
                direction=RecoveryDirection.AT_RISK.value,
                status=RecoveryStatus.OPEN.value,
                category=LeakageCategory.APPROVED_NOT_PAID.value,
                amount_paise=claim.amount_approved_paise,
                detected_at=timestamp,
                description=f"Claim {claim.reference} approved but no payout found in settlements.",
                is_sample=claim.is_sample,
            )
            db.add(entry)
            report.entries_created += 1
            report.detected_paise += entry.amount_paise
            report.by_category[LeakageCategory.APPROVED_NOT_PAID.value] = (
                report.by_category.get(LeakageCategory.APPROVED_NOT_PAID.value, 0)
                + entry.amount_paise
            )
    db.commit()
    return report


def mark_recovered(
    db: Session, entry: RecoveryEntry, *, when: datetime | None = None
) -> RecoveryEntry:
    if entry.status != RecoveryStatus.OPEN.value:
        raise SettlementError("Only open recovery entries can be marked recovered")
    entry.status = RecoveryStatus.RECOVERED.value
    entry.direction = RecoveryDirection.RECOVERED.value
    entry.recovered_at = when or datetime.now(UTC)
    db.commit()
    db.refresh(entry)
    return entry


def recovery_summary(
    db: Session, org_id: int, *, month: str | None = None
) -> RecoverySummary:
    entries = list(
        db.scalars(select(RecoveryEntry).where(RecoveryEntry.org_id == org_id)).all()
    )
    marketplace_names = {
        marketplace.id: marketplace.name
        for marketplace in db.scalars(select(Marketplace)).all()
    }
    buckets: dict[tuple[int | None, str], dict[str, int]] = {}
    for entry in entries:
        at_risk = (
            entry.direction == RecoveryDirection.AT_RISK.value
            and entry.status == RecoveryStatus.OPEN.value
        )
        recovered = entry.direction == RecoveryDirection.RECOVERED.value
        if not at_risk and not recovered:
            continue
        stamp = entry.detected_at
        if recovered and entry.recovered_at is not None:
            stamp = entry.recovered_at
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=UTC)
        entry_month = stamp.strftime("%Y-%m")
        if month and entry_month != month:
            continue
        key = (entry.marketplace_id, entry_month)
        bucket = buckets.setdefault(
            key,
            {"at_risk": 0, "recovered": 0, "open": 0, "recovered_count": 0},
        )
        if at_risk:
            bucket["at_risk"] += entry.amount_paise
            bucket["open"] += 1
        if recovered:
            bucket["recovered"] += entry.amount_paise
            bucket["recovered_count"] += 1
    rows = [
        RecoverySummaryRow(
            marketplace_id=marketplace_id,
            marketplace=marketplace_names.get(marketplace_id or 0, "Unassigned"),
            month=entry_month,
            at_risk_paise=values["at_risk"],
            recovered_paise=values["recovered"],
            open_entries=values["open"],
            recovered_entries=values["recovered_count"],
        )
        for (marketplace_id, entry_month), values in buckets.items()
    ]
    rows.sort(key=lambda row: (row.month, row.marketplace), reverse=True)
    return RecoverySummary(
        rows=rows,
        total_at_risk_paise=sum(row.at_risk_paise for row in rows),
        total_recovered_paise=sum(row.recovered_paise for row in rows),
    )
