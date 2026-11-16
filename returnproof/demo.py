"""Seed a demo database, add sample evidence/claims, and print the seller login.

Usage:
    python -m returnproof.demo --reset

The demo uses only synthetic data and local files. It never calls a marketplace
API and never starts an LLM.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from io import BytesIO
from typing import Any

from PIL import Image, ImageDraw
from sqlalchemy import select
from sqlalchemy.orm import Session

from returnproof.claims import create_claim, load_claim_templates
from returnproof.config import Settings, get_settings
from returnproof.db import build_engine, build_session_factory, create_schema
from returnproof.domain.models import (
    Base,
    Claim,
    Evidence,
    Order,
    Organisation,
    ReturnRequest,
    User,
)
from returnproof.evidence import append_evidence
from returnproof.seed import SAMPLE_PASSWORD, seed
from returnproof.storage import build_storage
from returnproof.verification import VerificationInput, verify_return


def demo_image(variant: int) -> bytes:
    image = Image.new("RGB", (640, 480), (245, 246, 248))
    draw = ImageDraw.Draw(image)
    if variant == 0:
        draw.rounded_rectangle([70, 90, 570, 390], radius=28, fill=(24, 43, 73))
        draw.ellipse([210, 150, 430, 330], fill=(230, 180, 70))
        draw.text((90, 430), "DISPATCH UNIT · sample evidence", fill=(60, 70, 85))
    else:
        draw.rounded_rectangle([110, 60, 530, 420], radius=8, fill=(190, 120, 90))
        draw.rectangle([180, 120, 460, 300], fill=(250, 250, 250))
        draw.text((90, 440), "RETURNED UNIT · sample evidence", fill=(60, 70, 85))
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _demo_order_returns(db: Session, org_id: int) -> list[tuple[Order, ReturnRequest]]:
    rows = db.execute(
        select(Order, ReturnRequest)
        .join(ReturnRequest, ReturnRequest.order_id == Order.id)
        .where(
            Order.org_id == org_id,
            ReturnRequest.received_at.is_not(None),
        )
        .order_by(Order.id)
        .limit(4)
    ).all()
    return [(order, record) for order, record in rows]


def populate_demo(db: Session, settings: Settings, organisation: Organisation) -> dict[str, Any]:
    owner = db.scalar(
        select(User).where(User.org_id == organisation.id, User.role == "owner").order_by(User.id)
    )
    if owner is None:
        raise RuntimeError("Demo organisation has no owner user")
    storage = build_storage(settings)
    templates = load_claim_templates()
    pairs = _demo_order_returns(db, organisation.id)
    evidence_added = 0
    verifications = 0
    claim_ref: str | None = None

    for index, (order, record) in enumerate(pairs):
        item = order.items[0] if order.items else None
        has_dispatch = db.scalar(
            select(Evidence.id).where(
                Evidence.order_id == order.id,
                Evidence.stage == "dispatch",
                Evidence.kind == "photo",
            )
        )
        if has_dispatch is None:
            dispatch_bytes = demo_image(0)
            append_evidence(
                db,
                storage,
                order=order,
                stage="dispatch",
                kind="photo",
                filename=f"dispatch-{order.order_number}.png",
                content_type="image/png",
                data=dispatch_bytes,
                uploader_id=owner.id,
                uploader_label=owner.full_name,
                device_note="Sample packing-bench camera",
                captured_at=order.dispatched_at or datetime.now(UTC),
                max_bytes=settings.max_upload_mb * 1024 * 1024,
            )
            evidence_added += 1
        has_receipt = db.scalar(
            select(Evidence.id).where(
                Evidence.return_request_id == record.id,
                Evidence.stage == "return_receipt",
                Evidence.kind == "photo",
            )
        )
        if has_receipt is None:
            return_bytes = demo_image(0 if index > 0 else 1)
            append_evidence(
                db,
                storage,
                order=order,
                stage="return_receipt",
                kind="photo",
                filename=f"return-{record.rma_number}.png",
                content_type="image/png",
                data=return_bytes,
                uploader_id=owner.id,
                uploader_label=owner.full_name,
                device_note="Sample warehouse intake camera",
                captured_at=record.received_at or datetime.now(UTC),
                return_request_id=record.id,
                max_bytes=settings.max_upload_mb * 1024 * 1024,
            )
            evidence_added += 1

        serial = item.serial_number if item else None
        submitted_serial = "SN-SWAPPED-DEMO" if index == 0 else serial
        market = order.marketplace
        code = market.config.get("template") if market.config else "generic"
        result = verify_return(
            db,
            return_request=record,
            payload=VerificationInput(
                identifiers={
                    "serial_number": submitted_serial,
                    "imei": item.imei if item else None,
                    "barcode": item.barcode if item else None,
                    "batch_code": item.batch_code if item else None,
                },
                seal_intact=True,
                accessories_present=index != 0,
                tags_present=True,
                weight_grams=item.expected_weight_grams if item else None,
            ),
            threshold=settings.phash_threshold,
            weight_tolerance_grams=settings.weight_tolerance_grams,
            weight_tolerance_percent=settings.weight_tolerance_percent,
            storage=storage,
            verifier_id=owner.id,
        )
        verifications += 1
        existing_claim = db.scalar(
            select(Claim).where(
                Claim.org_id == organisation.id,
                Claim.return_request_id == record.id,
            )
        )
        if result.status in {"mismatch", "needs_review"} and existing_claim is None:
            claim, _pack = create_claim(
                db,
                storage,
                settings,
                organisation=organisation,
                order=order,
                return_request=record,
                verification=result,
                marketplace=market,
                template_code=str(code) if code in templates else "generic",
                amount_claimed_paise=record.refund_amount_paise or order.total_paise,
                notes="Demo claim generated from synthetic sample evidence.",
                actor_id=owner.id,
            )
            claim_ref = claim.reference

    return {
        "evidence_added": evidence_added,
        "verifications": verifications,
        "claim_reference": claim_ref,
        "demo_email": owner.email,
        "demo_password": SAMPLE_PASSWORD,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a ReturnProof demo workspace.")
    parser.add_argument("--reset", action="store_true", help="Drop and recreate the demo database.")
    parser.add_argument("--database-url", default=None)
    args = parser.parse_args()

    settings: Settings = get_settings()
    database_url = args.database_url or settings.database_url
    engine = build_engine(database_url)
    if args.reset:
        Base.metadata.drop_all(engine)
    create_schema(engine)
    db = build_session_factory(engine)()
    try:
        organisation = db.scalar(select(Organisation).order_by(Organisation.id))
        if organisation is None:
            summary = seed(db)
            db.commit()
            organisation = db.scalar(select(Organisation).order_by(Organisation.id))
            if organisation is None:
                raise RuntimeError("Seed did not create an organisation")
        else:
            summary = None
        stats = populate_demo(db, settings, organisation)
    finally:
        db.close()
        engine.dispose()

    print()
    print("ReturnProof demo workspace ready (synthetic sample data only).")
    print("=" * 62)
    print("  Start:   uvicorn returnproof.app:app --reload --port 8000")
    print("  Open:    http://localhost:8000/login")
    print(f"  Email:   {stats['demo_email']}")
    print(f"  Password: {stats['demo_password']}")
    print("-" * 62)
    if summary is not None:
        print(
            f"  Seeded:  {summary.orders} orders · {summary.returns} returns · "
            f"{summary.claims} pre-existing claims"
        )
    print(
        f"  Demo:    {stats['evidence_added']} evidence files · "
        f"{stats['verifications']} verifications"
    )
    if stats.get("claim_reference"):
        print(f"  Claim:   {stats['claim_reference']} (open Claims in the app)")
    print("  Also try /app/settlements with samples/settlements/generic_settlement_sample.csv")
    print("=" * 62)


if __name__ == "__main__":
    main()
