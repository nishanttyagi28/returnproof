"""Claim creation, data-driven pack rendering, and lifecycle transitions."""

from __future__ import annotations

import base64
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape as xml_escape

from PIL import Image, UnidentifiedImageError
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    Image as PdfImage,
)
from reportlab.platypus import (
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

from returnproof.config import Settings
from returnproof.domain.enums import (
    ClaimStatus,
    EvidenceKind,
    RecoveryDirection,
    RecoveryStatus,
    VerificationStatus,
)
from returnproof.domain.models import (
    Claim,
    Evidence,
    Marketplace,
    Order,
    OrderItem,
    Organisation,
    RecoveryEntry,
    ReturnRequest,
    VerificationResult,
)
from returnproof.storage import Storage
from returnproof.web.templating import format_inr, format_ist, templates

TEMPLATE_PATH = Path(__file__).resolve().parent / "data" / "claim_templates.json"
DAMAGE_REASONS = {"damaged", "empty_box", "wrong_item", "not_as_described", "missing_accessories"}

CLAIM_TRANSITIONS: dict[str, set[str]] = {
    ClaimStatus.DRAFT.value: {ClaimStatus.FILED.value},
    ClaimStatus.FILED.value: {
        ClaimStatus.APPROVED.value,
        ClaimStatus.REJECTED.value,
        ClaimStatus.PARTIAL.value,
    },
}


class ClaimError(ValueError):
    """Base error for claim operations."""


class ClaimConflictError(ClaimError):
    """The claim is not in a state that permits the operation."""


class ClaimValidationError(ClaimError):
    """Invalid claim input or template."""


@dataclass
class PackResult:
    html_path: Path
    pdf_path: Path
    html: str


@lru_cache
def load_claim_templates() -> dict[str, dict[str, Any]]:
    raw = json.loads(TEMPLATE_PATH.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or "generic" not in raw:
        raise ClaimValidationError("Claim template registry is invalid")
    return raw


def template_for(marketplace: Marketplace, requested: str | None = None) -> tuple[str, dict[str, Any]]:
    registry = load_claim_templates()
    code = requested
    if not code:
        configured = marketplace.config.get("template") if marketplace.config else None
        code = configured if isinstance(configured, str) else "generic"
    if code not in registry:
        raise ClaimValidationError(f"Unknown claim template: {code}")
    return code, registry[code]


def claim_eligible(return_request: ReturnRequest, verification: VerificationResult | None) -> bool:
    if return_request.reason_code in DAMAGE_REASONS:
        return True
    return bool(
        verification is not None
        and verification.status
        in {VerificationStatus.MISMATCH.value, VerificationStatus.NEEDS_REVIEW.value}
    )


def _reference() -> str:
    stamp = datetime.now().strftime("%Y%m")
    return f"CLM-{stamp}-{uuid.uuid4().hex[:8].upper()}"


def _safe_component(value: str) -> str:
    text = "".join(ch for ch in value if ch.isalnum() or ch in {"-", "_"}).strip("-_")
    return text or "claim"


def _pack_base(settings: Settings, claim: Claim) -> Path:
    return settings.pack_dir.resolve() / f"org_{claim.org_id}" / _safe_component(claim.reference)


def pack_paths(settings: Settings, claim: Claim) -> tuple[Path, Path]:
    base = _pack_base(settings, claim)
    return base.with_suffix(".html"), base.with_suffix(".pdf")


def _thumb_data_uri(storage: Storage, evidence: Evidence) -> str | None:
    if evidence.kind != EvidenceKind.PHOTO.value:
        return None
    try:
        raw = storage.read(evidence.stored_path)
        with Image.open(BytesIO(raw)) as image:
            converted = image.convert("RGB")
            converted.thumbnail((520, 390))
            buffer = BytesIO()
            converted.save(buffer, format="JPEG", quality=82)
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
        return f"data:image/jpeg;base64,{encoded}"
    except (FileNotFoundError, UnidentifiedImageError, OSError, ValueError):
        return None


def _timeline(order: Order, ret: ReturnRequest, claim: Claim, verification: VerificationResult | None) -> list[dict[str, Any]]:
    events: list[tuple[str, datetime | None, str]] = [
        ("Order placed", order.placed_at, f"Marketplace order {order.order_number}"),
        ("Dispatch", order.dispatched_at, "Shipment handed to courier"),
        ("Delivery", order.delivered_at, "Delivery recorded by marketplace"),
        ("Return requested", ret.requested_at, ret.reason_text or ret.reason_code),
        ("Return received", ret.received_at, "Unit received at seller warehouse"),
    ]
    if verification is not None:
        events.append(
            (
                "Identity verification",
                verification.created_at,
                f"{verification.status.replace('_', ' ').title()} · score {verification.score:.2f}",
            )
        )
    if claim.filed_at:
        events.append(("Claim filed", claim.filed_at, f"Reference {claim.reference}"))
    return [
        {"label": label, "at": at, "note": note} for label, at, note in events if at is not None
    ]


def _pack_context(
    *,
    claim: Claim,
    organisation: Organisation,
    order: Order,
    marketplace: Marketplace,
    return_request: ReturnRequest,
    verification: VerificationResult | None,
    items: list[OrderItem],
    evidence: list[Evidence],
    template: dict[str, Any],
    storage: Storage,
) -> dict[str, Any]:
    photos = []
    for record in evidence:
        photos.append(
            {
                "id": record.id,
                "filename": record.filename,
                "kind": record.kind,
                "stage": record.stage,
                "captured_at": record.captured_at,
                "uploader_label": record.uploader_label,
                "device_note": record.device_note,
                "sha256": record.sha256,
                "chain_hash": record.chain_hash,
                "chain_index": record.chain_index,
                "data_uri": _thumb_data_uri(storage, record),
            }
        )
    return {
        "claim": claim,
        "organisation": organisation,
        "order": order,
        "marketplace": marketplace,
        "return_request": return_request,
        "verification": verification,
        "items": items,
        "evidence": photos,
        "template": template,
        "timeline": _timeline(order, return_request, claim, verification),
        "generated_at": datetime.now(),
        "format_inr": format_inr,
        "format_ist": format_ist,
    }


def build_claim_html(context: dict[str, Any]) -> str:
    template = templates.env.get_template("claim_pack.html")
    return template.render(**context)


def _pdf_styles(accent: str) -> dict[str, ParagraphStyle]:
    styles = getSampleStyleSheet()
    brand = colors.HexColor(accent)
    return {
        "title": ParagraphStyle(
            "ClaimTitle",
            parent=styles["Title"],
            fontName="Helvetica-Bold",
            fontSize=19,
            leading=23,
            textColor=brand,
            spaceAfter=4,
        ),
        "subtitle": ParagraphStyle(
            "ClaimSubtitle",
            parent=styles["Normal"],
            fontSize=9.5,
            leading=13,
            textColor=colors.HexColor("#475467"),
            spaceAfter=12,
        ),
        "h2": ParagraphStyle(
            "ClaimH2",
            parent=styles["Heading2"],
            fontSize=12,
            leading=15,
            textColor=brand,
            spaceBefore=12,
            spaceAfter=5,
        ),
        "body": ParagraphStyle(
            "ClaimBody", parent=styles["BodyText"], fontSize=8.8, leading=12
        ),
        "small": ParagraphStyle(
            "ClaimSmall",
            parent=styles["BodyText"],
            fontSize=7.6,
            leading=10,
            textColor=colors.HexColor("#475467"),
        ),
        "cell": ParagraphStyle(
            "ClaimCell", parent=styles["BodyText"], fontSize=8.2, leading=10.5
        ),
    }


def _pdf_table(rows: list[list[str]], accent: str, widths: list[float] | None = None) -> Table:
    body = [[Paragraph(xml_escape(str(cell)), _pdf_styles(accent)["cell"]) for cell in row] for row in rows]
    table = Table(body, colWidths=widths, hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f2f6f5")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#101828")),
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#d0d5dd")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]
        )
    )
    return table


def _pdf_thumbnail(storage: Storage, evidence: Evidence) -> PdfImage | None:
    if evidence.kind != EvidenceKind.PHOTO.value:
        return None
    try:
        raw = storage.read(evidence.stored_path)
        with Image.open(BytesIO(raw)) as image:
            converted = image.convert("RGB")
            converted.thumbnail((700, 525))
            buffer = BytesIO()
            converted.save(buffer, format="JPEG", quality=80)
            width, height = converted.size
        scale = min(70 * mm / width, 55 * mm / height)
        return PdfImage(BytesIO(buffer.getvalue()), width=width * scale, height=height * scale)
    except (FileNotFoundError, UnidentifiedImageError, OSError, ValueError):
        return None


def build_claim_pdf(context: dict[str, Any], storage: Storage) -> bytes:
    template = context["template"]
    accent = str(template.get("accent") or "#0b5c4b")
    styles = _pdf_styles(accent)
    buffer = BytesIO()

    def footer(canvas: Any, doc: Any) -> None:
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(colors.HexColor("#667085"))
        canvas.drawString(
            18 * mm,
            12 * mm,
            f"ReturnProof · {context['claim'].reference} · generated {format_ist(context['generated_at'])}",
        )
        canvas.drawRightString(192 * mm, 12 * mm, f"Page {doc.page}")
        canvas.restoreState()

    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=16 * mm,
        bottomMargin=18 * mm,
        title=f"{template['name']} · {context['claim'].reference}",
        author="ReturnProof",
        subject="Return evidence claim pack",
    )
    story: list[Any] = []
    claim = context["claim"]
    org = context["organisation"]
    order = context["order"]
    ret = context["return_request"]
    verification = context["verification"]

    story.append(Paragraph(xml_escape(str(template["title"])), styles["title"]))
    story.append(Paragraph(xml_escape(str(template["subtitle"])), styles["subtitle"]))
    if claim.is_sample:
        story.append(
            Paragraph(
                "SAMPLE DATA — this pack was generated from synthetic demo records.",
                styles["small"],
            )
        )
        story.append(Spacer(1, 4))

    summary_rows = [
        ["Claim reference", claim.reference, "Status", claim.status.replace("_", " ").title()],
        ["Template", str(template["name"]), "Marketplace", context["marketplace"].name],
        [
            "Amount claimed",
            format_inr(claim.amount_claimed_paise),
            "Deadline",
            format_ist(claim.deadline_at),
        ],
        ["Seller", org.legal_name or org.name, "GSTIN", org.gstin or "—"],
    ]
    story.append(_pdf_table(summary_rows, accent, widths=[32 * mm, 55 * mm, 27 * mm, 60 * mm]))
    story.append(Spacer(1, 8))

    story.append(Paragraph("Order", styles["h2"]))
    story.append(
        _pdf_table(
            [
                ["Order", order.order_number, "Placed", format_ist(order.placed_at)],
                ["Buyer", order.customer_name, "Pincode", order.customer_pincode or "—"],
                ["Order total", format_inr(order.total_paise), "Return", ret.rma_number],
            ],
            accent,
            widths=[28 * mm, 55 * mm, 28 * mm, 63 * mm],
        )
    )
    if context["items"]:
        story.append(Spacer(1, 6))
        rows = [["SKU", "Item", "Qty", "Unit price", "Serial / IMEI", "Expected weight"]]
        for item in context["items"]:
            rows.append(
                [
                    item.sku_snapshot,
                    item.title_snapshot,
                    item.quantity,
                    format_inr(item.unit_price_paise),
                    item.serial_number or item.imei or "—",
                    f"{item.expected_weight_grams or '—'} g",
                ]
            )
        story.append(_pdf_table(rows, accent))

    story.append(Paragraph("Timeline", styles["h2"]))
    for event in context["timeline"]:
        story.append(
            Paragraph(
                f"<b>{xml_escape(event['label'])}</b> · {format_ist(event['at'])} — "
                f"{xml_escape(str(event['note']))}",
                styles["body"],
            )
        )

    story.append(Paragraph("Verification findings", styles["h2"]))
    if verification is None:
        story.append(Paragraph("No verification result recorded.", styles["body"]))
    else:
        story.append(
            Paragraph(
                f"<b>Outcome:</b> {verification.status.replace('_', ' ').title()} · "
                f"score {verification.score:.2f} · image distance "
                f"{verification.image_distance if verification.image_distance is not None else 'n/a'}",
                styles["body"],
            )
        )
        for reason in verification.reasons:
            story.append(Paragraph(f"• {xml_escape(str(reason))}", styles["body"]))
        identifiers = verification.identifier_checks or {}
        if identifiers:
            rows = [["Identifier", "Expected", "Returned", "Result"]]
            for check in identifiers.values():
                rows.append(
                    [
                        check.get("label", ""),
                        check.get("expected") or "—",
                        check.get("submitted") or "—",
                        check.get("status", "").replace("_", " ").title(),
                    ]
                )
            story.append(_pdf_table(rows, accent))

    story.append(Paragraph("Evidence", styles["h2"]))
    thumbnails: list[Any] = []
    for record in context["evidence"]:
        evidence_entity = next(
            (item for item in context.get("evidence_entities", []) if item.id == record["id"]), None
        )
        if evidence_entity is not None:
            image = _pdf_thumbnail(storage, evidence_entity)
            if image is not None:
                thumbnails.append(
                    KeepTogether(
                        [
                            image,
                            Paragraph(
                                f"{xml_escape(record['filename'])} · {record['stage']} · "
                                f"SHA-256 {record['sha256'][:20]}…",
                                styles["small"],
                            ),
                            Spacer(1, 5),
                        ]
                    )
                )
    if thumbnails:
        story.extend(thumbnails[:6])
    rows = [["#", "File", "Stage", "SHA-256", "Chain hash"]]
    for record in context["evidence"]:
        rows.append(
            [
                record["chain_index"],
                record["filename"],
                record["stage"],
                record["sha256"][:16] + "…",
                record["chain_hash"][:16] + "…",
            ]
        )
    if len(rows) > 1:
        story.append(_pdf_table(rows, accent))
    else:
        story.append(Paragraph("No evidence files recorded.", styles["body"]))

    if "declaration" in template.get("sections", []):
        story.append(Paragraph("Declaration", styles["h2"]))
        story.append(Paragraph(xml_escape(str(template.get("declaration", ""))), styles["body"]))
    notes = template.get("submission_notes") or []
    if notes and "notes" in template.get("sections", []):
        story.append(Paragraph("Submission notes", styles["h2"]))
        for note in notes:
            story.append(Paragraph(f"• {xml_escape(str(note))}", styles["body"]))
    legal = template.get("legal_note")
    if legal:
        story.append(Spacer(1, 8))
        story.append(Paragraph(xml_escape(str(legal)), styles["small"]))

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buffer.getvalue()


def generate_claim_pack(
    db: Session,
    storage: Storage,
    settings: Settings,
    *,
    claim: Claim,
    organisation: Organisation,
    order: Order,
    marketplace: Marketplace,
    return_request: ReturnRequest,
    verification: VerificationResult | None,
    items: list[OrderItem],
    evidence: list[Evidence],
) -> PackResult:
    template = load_claim_templates()[claim.template_code]
    context = _pack_context(
        claim=claim,
        organisation=organisation,
        order=order,
        marketplace=marketplace,
        return_request=return_request,
        verification=verification,
        items=items,
        evidence=evidence,
        template=template,
        storage=storage,
    )
    context["evidence_entities"] = evidence
    html = build_claim_html(context)
    try:
        pdf = build_claim_pdf(context, storage)
    except Exception as exc:  # reportlab can fail on unusual images; never lose the claim
        raise ClaimValidationError(f"Could not render claim PDF: {exc}") from exc

    html_path, pdf_path = pack_paths(settings, claim)
    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(html, encoding="utf-8")
    pdf_path.write_bytes(pdf)
    claim.pack_path = str(html_path.relative_to(settings.pack_dir.resolve()))
    db.commit()
    return PackResult(html_path=html_path, pdf_path=pdf_path, html=html)


def create_claim(
    db: Session,
    storage: Storage,
    settings: Settings,
    *,
    organisation: Organisation,
    order: Order,
    return_request: ReturnRequest,
    verification: VerificationResult | None,
    marketplace: Marketplace,
    template_code: str | None,
    amount_claimed_paise: int | None,
    notes: str | None,
    actor_id: int | None,
) -> tuple[Claim, PackResult]:
    if not claim_eligible(return_request, verification):
        raise ClaimConflictError(
            "A claim needs a MISMATCH/NEEDS_REVIEW verification or a damage-related return reason"
        )
    code, _template = template_for(marketplace, template_code)
    amount = amount_claimed_paise or return_request.refund_amount_paise or order.total_paise
    if amount <= 0:
        raise ClaimValidationError("Claim amount must be positive")
    claim = Claim(
        org_id=organisation.id,
        order_id=order.id,
        return_request_id=return_request.id,
        marketplace_id=marketplace.id,
        reference=_reference(),
        status=ClaimStatus.DRAFT.value,
        template_code=code,
        amount_claimed_paise=amount,
        amount_approved_paise=0,
        notes=notes,
        is_sample=order.is_sample,
    )
    db.add(claim)
    db.flush()
    items = list(
        db.scalars(select(OrderItem).where(OrderItem.order_id == order.id).order_by(OrderItem.id)).all()
    )
    evidence = list(
        db.scalars(
            select(Evidence)
            .where(Evidence.org_id == organisation.id, Evidence.order_id == order.id)
            .order_by(Evidence.chain_index)
        ).all()
    )
    pack = generate_claim_pack(
        db,
        storage,
        settings,
        claim=claim,
        organisation=organisation,
        order=order,
        marketplace=marketplace,
        return_request=return_request,
        verification=verification,
        items=items,
        evidence=evidence,
    )
    return claim, pack




def file_claim(db: Session, claim: Claim, *, filed_at: datetime | None = None) -> Claim:
    if claim.status != ClaimStatus.DRAFT.value:
        raise ClaimConflictError("Only draft claims can be filed")
    timestamp = filed_at or datetime.now(UTC)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    marketplace = claim.marketplace
    claim.status = ClaimStatus.FILED.value
    claim.filed_at = timestamp
    claim.deadline_at = timestamp + timedelta(days=marketplace.claim_window_days)
    db.commit()
    db.refresh(claim)
    return claim


def resolve_claim(
    db: Session,
    claim: Claim,
    *,
    status: str,
    amount_approved_paise: int | None = None,
    resolved_at: datetime | None = None,
) -> Claim:
    if claim.status != ClaimStatus.FILED.value:
        raise ClaimConflictError("Only filed claims can be resolved")
    allowed = {
        ClaimStatus.APPROVED.value,
        ClaimStatus.REJECTED.value,
        ClaimStatus.PARTIAL.value,
    }
    if status not in allowed:
        raise ClaimValidationError(f"Invalid resolution status: {status}")
    if status == ClaimStatus.REJECTED.value:
        approved = 0
    elif status == ClaimStatus.PARTIAL.value:
        approved = amount_approved_paise or 0
        if approved <= 0 or approved >= claim.amount_claimed_paise:
            raise ClaimValidationError(
                "A partial approval must be greater than zero and below the claimed amount"
            )
    else:
        approved = (
            amount_approved_paise
            if amount_approved_paise is not None
            else claim.amount_claimed_paise
        )
        if approved <= 0:
            raise ClaimValidationError("Approved amount must be positive")
    claim.status = status
    claim.amount_approved_paise = approved
    claim.resolved_at = resolved_at or datetime.now(UTC)
    db.add(
        RecoveryEntry(
            org_id=claim.org_id,
            claim_id=claim.id,
            order_id=claim.order_id,
            return_request_id=claim.return_request_id,
            marketplace_id=claim.marketplace_id,
            direction=(
                RecoveryDirection.RECOVERED.value
                if approved > 0
                else RecoveryDirection.AT_RISK.value
            ),
            status=(
                RecoveryStatus.RECOVERED.value if approved > 0 else RecoveryStatus.OPEN.value
            ),
            category="claim_payout",
            amount_paise=approved,
            recovered_at=claim.resolved_at if approved > 0 else None,
            description=f"Claim {claim.reference} resolved as {status}",
            is_sample=claim.is_sample,
        )
    )
    db.commit()
    db.refresh(claim)
    return claim
