"""Product-identity verification for returned units.

The verifier combines three independent signals:

* typed identifiers from dispatch records versus values captured at return,
* perceptual-hash distance between dispatch and return photographs,
* a physical checklist (seal, accessories, tags, weight).

Identifiers are authoritative: any mismatch is a hard mismatch. Missing data or
unavailable photos produce NEEDS_REVIEW rather than a false MATCH.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from typing import Any

from PIL import Image, UnidentifiedImageError
from sqlalchemy import select
from sqlalchemy.orm import Session

from returnproof.domain.enums import VerificationStatus
from returnproof.domain.models import Evidence, Order, OrderItem, ReturnRequest, VerificationResult
from returnproof.storage import Storage

IDENTIFIER_FIELDS: tuple[tuple[str, str], ...] = (
    ("serial_number", "Serial number"),
    ("imei", "IMEI"),
    ("barcode", "Barcode"),
    ("batch_code", "Batch code"),
)


@dataclass
class VerificationInput:
    identifiers: dict[str, str | None] = field(default_factory=dict)
    seal_intact: bool | None = None
    accessories_present: bool | None = None
    tags_present: bool | None = None
    weight_grams: int | None = None


@dataclass
class CsvIssue:
    row_number: int
    message: str


@dataclass
class ParsedCsvRow:
    row_number: int
    rma_number: str
    order_number: str
    payload: VerificationInput


def normalize_identifier(value: str | None) -> str:
    if not value:
        return ""
    return "".join(ch for ch in value.strip().upper() if ch not in {" ", "-", "_"})


def _identifier_checks(
    expected: dict[str, str | None], submitted: dict[str, str | None]
) -> tuple[dict[str, Any], list[str], bool, bool, int]:
    checks: dict[str, Any] = {}
    reasons: list[str] = []
    hard_mismatch = False
    review = False
    checked = 0
    for key, label in IDENTIFIER_FIELDS:
        expected_value = normalize_identifier(expected.get(key))
        submitted_value = normalize_identifier(submitted.get(key))
        if not expected_value and not submitted_value:
            continue
        checked += 1
        if not submitted_value:
            status = "missing"
            review = True
            reasons.append(f"{label} was not captured at return.")
        elif not expected_value:
            status = "not_expected"
            review = True
            reasons.append(f"{label} was captured at return but has no dispatch reference.")
        elif expected_value == submitted_value:
            status = "match"
        else:
            status = "mismatch"
            hard_mismatch = True
            reasons.append(
                f"{label} does not match the dispatch record "
                f"(expected {expected.get(key)}, received {submitted.get(key)})."
            )
        checks[key] = {
            "label": label,
            "expected": expected.get(key),
            "submitted": submitted.get(key),
            "status": status,
        }
    return checks, reasons, hard_mismatch, review, checked


def _checklist(
    payload: VerificationInput,
    expected_weight: int | None,
    *,
    tolerance_grams: int,
    tolerance_percent: float,
) -> tuple[dict[str, Any], list[str], bool, bool, float | None]:
    checklist: dict[str, Any] = {
        "seal_intact": payload.seal_intact,
        "accessories_present": payload.accessories_present,
        "tags_present": payload.tags_present,
        "weight_grams": payload.weight_grams,
        "expected_weight_grams": expected_weight,
        "weight_ok": None,
        "weight_variance_grams": None,
        "weight_tolerance_grams": None,
    }
    reasons: list[str] = []
    hard_mismatch = False
    review = False
    assessed = 0
    passed = 0

    for key, label in (
        ("seal_intact", "Seal"),
        ("accessories_present", "Accessories"),
        ("tags_present", "Tags"),
    ):
        value = getattr(payload, key)
        if value is None:
            review = True
            reasons.append(f"{label} condition was not recorded.")
            continue
        assessed += 1
        if value:
            passed += 1
        else:
            hard_mismatch = True
            reasons.append(f"{label} check failed for the returned unit.")

    if payload.weight_grams is None:
        review = True
        reasons.append("Returned weight was not recorded.")
    elif expected_weight is None or expected_weight <= 0:
        review = True
        reasons.append("No expected dispatch weight is configured for this item.")
    else:
        tolerance = max(tolerance_grams, int(expected_weight * tolerance_percent / 100))
        variance = abs(payload.weight_grams - expected_weight)
        weight_ok = variance <= tolerance
        checklist.update(
            weight_ok=weight_ok,
            weight_variance_grams=variance,
            weight_tolerance_grams=tolerance,
        )
        assessed += 1
        if weight_ok:
            passed += 1
        else:
            hard_mismatch = True
            reasons.append(
                f"Returned weight {payload.weight_grams} g is outside "
                f"{expected_weight} g ± {tolerance} g."
            )

    score = (passed / assessed) if assessed else None
    return checklist, reasons, hard_mismatch, review, score


def _perceptual_hash(storage: Storage | None, evidence: Evidence) -> str | None:
    if evidence.perceptual_hash:
        return evidence.perceptual_hash
    if storage is None:
        return None
    try:
        import imagehash

        with Image.open(io.BytesIO(storage.read(evidence.stored_path))) as image:
            return str(imagehash.dhash(image))
    except (FileNotFoundError, UnidentifiedImageError, OSError, ValueError):
        return None


def hash_distance(first: str, second: str) -> int:
    try:
        return (int(first, 16) ^ int(second, 16)).bit_count()
    except ValueError:
        return 64


def _photos(
    db: Session, return_request: ReturnRequest
) -> tuple[Evidence | None, Evidence | None]:
    def latest(stage: str, return_id: int | None) -> Evidence | None:
        stmt = select(Evidence).where(
            Evidence.org_id == return_request.org_id,
            Evidence.order_id == return_request.order_id,
            Evidence.stage == stage,
            Evidence.kind == "photo",
        )
        if return_id is not None:
            stmt = stmt.where(Evidence.return_request_id == return_id)
        return db.scalar(stmt.order_by(Evidence.captured_at.desc(), Evidence.id.desc()).limit(1))

    return latest("dispatch", None), latest("return_receipt", return_request.id)


def verify_return(
    db: Session,
    *,
    return_request: ReturnRequest,
    payload: VerificationInput,
    threshold: int = 8,
    weight_tolerance_grams: int = 25,
    weight_tolerance_percent: float = 5.0,
    storage: Storage | None = None,
    verifier_id: int | None = None,
) -> VerificationResult:
    """Run all checks, upsert the verification result and return it."""
    order = db.get(Order, return_request.order_id)
    if order is None:
        raise ValueError("Return request has no order")
    items = list(db.scalars(select(OrderItem).where(OrderItem.order_id == order.id)).all())
    expected_identifiers = {
        "serial_number": next((item.serial_number for item in items if item.serial_number), None),
        "imei": next((item.imei for item in items if item.imei), None),
        "barcode": next((item.barcode for item in items if item.barcode), None),
        "batch_code": next((item.batch_code for item in items if item.batch_code), None),
    }
    expected_weight = sum(item.expected_weight_grams or 0 for item in items) or None

    checks, identifier_reasons, id_mismatch, id_review, checked = _identifier_checks(
        expected_identifiers, payload.identifiers
    )
    checklist, checklist_reasons, list_mismatch, list_review, checklist_score = _checklist(
        payload,
        expected_weight,
        tolerance_grams=weight_tolerance_grams,
        tolerance_percent=weight_tolerance_percent,
    )

    dispatch_photo, return_photo = _photos(db, return_request)
    image_distance: int | None = None
    image_score: float | None = None
    image_review = False
    image_mismatch = False
    image_reasons: list[str] = []
    if dispatch_photo is None:
        image_review = True
        image_reasons.append("No dispatch photograph is available for comparison.")
    if return_photo is None:
        image_review = True
        image_reasons.append("No return-receipt photograph is available for comparison.")
    if dispatch_photo is not None and return_photo is not None:
        dispatch_hash = _perceptual_hash(storage, dispatch_photo)
        return_hash = _perceptual_hash(storage, return_photo)
        if not dispatch_hash or not return_hash:
            image_review = True
            image_reasons.append("A photograph could not be hashed for visual comparison.")
        else:
            image_distance = hash_distance(dispatch_hash, return_hash)
            image_score = max(0.0, 1.0 - image_distance / 32.0)
            if image_distance > threshold:
                image_mismatch = True
                image_reasons.append(
                    f"Visual hash distance {image_distance} exceeds the configured "
                    f"threshold of {threshold}."
                )

    identifier_score: float | None = None
    if checked:
        matched = sum(1 for check in checks.values() if check["status"] == "match")
        identifier_score = matched / checked

    components: list[tuple[float, float]] = []
    if identifier_score is not None:
        components.append((identifier_score, 0.45))
    if image_score is not None:
        components.append((image_score, 0.35))
    if checklist_score is not None:
        components.append((checklist_score, 0.20))
    if components:
        total_weight = sum(weight for _, weight in components)
        score = sum(value * weight for value, weight in components) / total_weight
    else:
        score = 0.0

    if id_mismatch or list_mismatch or image_mismatch:
        status = VerificationStatus.MISMATCH
    elif id_review or list_review or image_review:
        status = VerificationStatus.NEEDS_REVIEW
    else:
        status = VerificationStatus.MATCH

    reasons = identifier_reasons + image_reasons + checklist_reasons
    if status == VerificationStatus.MATCH:
        reasons = ["All captured identifier, visual and checklist checks agree."]
    elif not reasons:
        reasons = ["Insufficient evidence to verify the returned unit."]

    existing = db.scalar(
        select(VerificationResult).where(
            VerificationResult.return_request_id == return_request.id,
            VerificationResult.org_id == return_request.org_id,
        )
    )
    target = existing or VerificationResult(
        org_id=return_request.org_id,
        order_id=return_request.order_id,
        return_request_id=return_request.id,
    )
    target.status = status.value
    target.score = round(score, 4)
    target.reasons = reasons
    target.checklist = checklist
    target.identifier_checks = checks
    target.returned_identifiers = {key: value for key, value in payload.identifiers.items() if value}
    target.image_distance = image_distance
    target.dispatch_evidence_id = dispatch_photo.id if dispatch_photo else None
    target.return_evidence_id = return_photo.id if return_photo else None
    target.verified_by_id = verifier_id
    if existing is None:
        db.add(target)
    db.commit()
    db.refresh(target)
    return target


def _parse_bool(value: str | None) -> bool | None:
    if value is None or not value.strip():
        return None
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "y", "intact", "present", "ok"}:
        return True
    if normalized in {"0", "false", "no", "n", "missing", "absent", "broken"}:
        return False
    raise ValueError(f"Cannot interpret boolean value {value!r}")


def parse_verification_csv(text: str) -> tuple[list[ParsedCsvRow], list[CsvIssue]]:
    """Parse the documented verification CSV format.

    Headers are case-insensitive. At least one of ``rma_number`` or
    ``order_number`` is required on every row.
    """
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    if reader.fieldnames is None:
        return [], [CsvIssue(0, "CSV has no header row")]
    aliases = {
        "rma_number": {"rma_number", "rma"},
        "order_number": {"order_number", "order"},
        "serial_number": {"serial_number", "serial"},
        "imei": {"imei"},
        "barcode": {"barcode"},
        "batch_code": {"batch_code", "batch"},
        "weight_grams": {"weight_grams", "weight"},
        "seal_intact": {"seal_intact", "seal"},
        "accessories_present": {"accessories_present", "accessories"},
        "tags_present": {"tags_present", "tags"},
    }
    header_map: dict[str, str] = {}
    for canonical, options in aliases.items():
        for fieldname in reader.fieldnames:
            if fieldname.strip().lower() in options:
                header_map[canonical] = fieldname
                break
    if "rma_number" not in header_map and "order_number" not in header_map:
        return [], [CsvIssue(0, "CSV needs an rma_number or order_number column")]

    rows: list[ParsedCsvRow] = []
    issues: list[CsvIssue] = []

    def value(row: dict[str, str | None], key: str) -> str:
        header = header_map.get(key)
        return (row.get(header) or "").strip() if header else ""

    for row_number, row in enumerate(reader, start=2):
        try:
            weight_raw = value(row, "weight_grams")
            payload = VerificationInput(
                identifiers={
                    key: value(row, key) or None
                    for key in ("serial_number", "imei", "barcode", "batch_code")
                },
                seal_intact=_parse_bool(value(row, "seal_intact")),
                accessories_present=_parse_bool(value(row, "accessories_present")),
                tags_present=_parse_bool(value(row, "tags_present")),
                weight_grams=int(weight_raw) if weight_raw else None,
            )
            parsed = ParsedCsvRow(
                row_number=row_number,
                rma_number=value(row, "rma_number"),
                order_number=value(row, "order_number"),
                payload=payload,
            )
            if not parsed.rma_number and not parsed.order_number:
                issues.append(CsvIssue(row_number, "Row has neither RMA nor order number"))
                continue
            rows.append(parsed)
        except ValueError as exc:
            issues.append(CsvIssue(row_number, str(exc)))
    return rows, issues

