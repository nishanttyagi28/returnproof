"""Evidence capture, append-only hash chain, and tamper verification."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

from PIL import Image, UnidentifiedImageError
from sqlalchemy import select
from sqlalchemy.orm import Session

from returnproof.domain.enums import EvidenceKind, EvidenceStage
from returnproof.domain.models import Evidence, Order, ReturnRequest, utcnow
from returnproof.storage import Storage, StorageError, make_storage_key

GENESIS_HASH = "0" * 64
CHAIN_VERSION = 1

ALLOWED_CONTENT: dict[str, set[str]] = {
    EvidenceKind.PHOTO.value: {"image/jpeg", "image/png", "image/webp"},
    EvidenceKind.VIDEO.value: {"video/mp4", "video/quicktime", "video/webm"},
    EvidenceKind.DOCUMENT.value: {
        "application/pdf",
        "text/csv",
        "text/plain",
        "application/json",
    },
}


class EvidenceValidationError(ValueError):
    """Invalid evidence metadata or file content."""


@dataclass
class ChainProblem:
    evidence_id: int | None
    code: str
    message: str


@dataclass
class ChainReport:
    order_id: int
    evidence_count: int
    valid: bool
    head_hash: str | None
    checked_at: datetime
    problems: list[ChainProblem] = field(default_factory=list)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def canonical_payload(
    *,
    order_id: int,
    chain_index: int,
    stage: str,
    kind: str,
    filename: str,
    content_type: str,
    size_bytes: int,
    sha256: str,
    previous_hash: str,
    captured_at: datetime,
    uploader_label: str,
    device_note: str | None,
) -> dict[str, object]:
    return {
        "v": CHAIN_VERSION,
        "order_id": order_id,
        "chain_index": chain_index,
        "stage": stage,
        "kind": kind,
        "filename": filename,
        "content_type": content_type,
        "size_bytes": size_bytes,
        "sha256": sha256,
        "previous_hash": previous_hash,
        "captured_at": _utc(captured_at).isoformat(timespec="microseconds"),
        "uploader_label": uploader_label,
        "device_note": device_note or "",
    }


def compute_chain_hash(
    *,
    order_id: int,
    chain_index: int,
    stage: str,
    kind: str,
    filename: str,
    content_type: str,
    size_bytes: int,
    sha256: str,
    previous_hash: str,
    captured_at: datetime,
    uploader_label: str,
    device_note: str | None,
) -> str:
    payload = canonical_payload(
        order_id=order_id,
        chain_index=chain_index,
        stage=stage,
        kind=kind,
        filename=filename,
        content_type=content_type,
        size_bytes=size_bytes,
        sha256=sha256,
        previous_hash=previous_hash,
        captured_at=captured_at,
        uploader_label=uploader_label,
        device_note=device_note,
    )
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _image_metadata(data: bytes) -> tuple[int, int, str | None]:
    try:
        import imagehash

        with Image.open(BytesIO(data)) as image:
            image.load()
            width, height = image.size
            perceptual = str(imagehash.dhash(image))
        return width, height, perceptual
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise EvidenceValidationError("Photo file is not a readable image") from exc


def _clean_filename(filename: str) -> str:
    name = Path(filename or "evidence").name.strip()
    name = "".join(ch for ch in name if ch.isprintable() and ch not in {'"', "\r", "\n"})
    return (name or "evidence")[:255]


def append_evidence(
    db: Session,
    storage: Storage,
    *,
    order: Order,
    stage: str,
    kind: str,
    filename: str,
    content_type: str,
    data: bytes,
    uploader_id: int | None,
    uploader_label: str,
    device_note: str | None = None,
    captured_at: datetime | None = None,
    return_request_id: int | None = None,
    max_bytes: int = 50 * 1024 * 1024,
) -> Evidence:
    """Validate, store and chain-link one evidence file. Caller owns tenancy checks."""
    if stage not in {item.value for item in EvidenceStage}:
        raise EvidenceValidationError("Unknown evidence stage")
    if kind not in {item.value for item in EvidenceKind}:
        raise EvidenceValidationError("Unknown evidence kind")
    if not data:
        raise EvidenceValidationError("File is empty")
    if len(data) > max_bytes:
        raise EvidenceValidationError(f"File exceeds the {max_bytes // (1024 * 1024)} MB limit")

    normalized_type = content_type.split(";", 1)[0].strip().lower()
    if normalized_type not in ALLOWED_CONTENT[kind]:
        raise EvidenceValidationError(f"Content type {normalized_type!r} is not allowed for {kind}")

    if stage == EvidenceStage.RETURN_RECEIPT.value:
        if return_request_id is None:
            raise EvidenceValidationError("Return receipt evidence must reference a return request")
        linked = db.scalar(
            select(ReturnRequest).where(
                ReturnRequest.id == return_request_id,
                ReturnRequest.order_id == order.id,
                ReturnRequest.org_id == order.org_id,
            )
        )
        if linked is None:
            raise EvidenceValidationError("Return request does not belong to this order")

    clean_name = _clean_filename(filename)
    sha256 = hashlib.sha256(data).hexdigest()
    width: int | None = None
    height: int | None = None
    perceptual: str | None = None
    if kind == EvidenceKind.PHOTO.value:
        width, height, perceptual = _image_metadata(data)

    previous = db.scalar(
        select(Evidence)
        .where(Evidence.order_id == order.id, Evidence.org_id == order.org_id)
        .order_by(Evidence.chain_index.desc())
        .limit(1)
    )
    chain_index = 0 if previous is None else previous.chain_index + 1
    previous_hash = GENESIS_HASH if previous is None else previous.chain_hash
    captured = _utc(captured_at or utcnow())
    chain_hash = compute_chain_hash(
        order_id=order.id,
        chain_index=chain_index,
        stage=stage,
        kind=kind,
        filename=clean_name,
        content_type=normalized_type,
        size_bytes=len(data),
        sha256=sha256,
        previous_hash=previous_hash,
        captured_at=captured,
        uploader_label=uploader_label,
        device_note=device_note,
    )

    key = make_storage_key(order.org_id, order.id, clean_name)
    storage.save(key, data)
    evidence = Evidence(
        org_id=order.org_id,
        order_id=order.id,
        return_request_id=return_request_id,
        stage=stage,
        kind=kind,
        chain_index=chain_index,
        filename=clean_name,
        stored_path=key,
        content_type=normalized_type,
        size_bytes=len(data),
        sha256=sha256,
        previous_hash=previous_hash,
        chain_hash=chain_hash,
        captured_at=captured,
        uploaded_by_id=uploader_id,
        uploader_label=uploader_label,
        device_note=(device_note or "").strip()[:240] or None,
        width=width,
        height=height,
        perceptual_hash=perceptual,
        is_sample=order.is_sample,
    )
    try:
        db.add(evidence)
        db.commit()
        db.refresh(evidence)
    except Exception:
        db.rollback()
        storage.delete(key)
        raise
    return evidence


def verify_order_chain(db: Session, storage: Storage, org_id: int, order_id: int) -> ChainReport:
    """Recompute file digests and the order's append-only evidence chain."""
    rows = list(
        db.scalars(
            select(Evidence)
            .where(Evidence.org_id == org_id, Evidence.order_id == order_id)
            .order_by(Evidence.chain_index)
        ).all()
    )
    problems: list[ChainProblem] = []
    expected_previous = GENESIS_HASH
    for expected_index, row in enumerate(rows):
        if row.chain_index != expected_index:
            problems.append(
                ChainProblem(row.id, "CHAIN_INDEX_GAP", f"Expected index {expected_index}, got {row.chain_index}")
            )
        if row.previous_hash != expected_previous:
            problems.append(
                ChainProblem(row.id, "CHAIN_LINK_BROKEN", "Previous hash does not match the preceding record")
            )
        try:
            data = storage.read(row.stored_path)
        except (FileNotFoundError, StorageError, OSError):
            problems.append(ChainProblem(row.id, "FILE_MISSING", "Evidence file is missing from storage"))
            expected_previous = row.chain_hash
            continue
        digest = hashlib.sha256(data).hexdigest()
        if digest != row.sha256:
            problems.append(
                ChainProblem(row.id, "FILE_HASH_MISMATCH", "Stored file no longer matches its SHA-256")
            )
        if len(data) != row.size_bytes:
            problems.append(
                ChainProblem(row.id, "FILE_SIZE_MISMATCH", "Stored file size differs from the recorded size")
            )
        recomputed = compute_chain_hash(
            order_id=row.order_id,
            chain_index=row.chain_index,
            stage=row.stage,
            kind=row.kind,
            filename=row.filename,
            content_type=row.content_type,
            size_bytes=row.size_bytes,
            sha256=row.sha256,
            previous_hash=row.previous_hash,
            captured_at=row.captured_at,
            uploader_label=row.uploader_label,
            device_note=row.device_note,
        )
        if recomputed != row.chain_hash:
            problems.append(
                ChainProblem(row.id, "CHAIN_HASH_MISMATCH", "Evidence metadata was altered after capture")
            )
        expected_previous = row.chain_hash

    return ChainReport(
        order_id=order_id,
        evidence_count=len(rows),
        valid=not problems,
        head_hash=rows[-1].chain_hash if rows else None,
        checked_at=utcnow(),
        problems=problems,
    )
