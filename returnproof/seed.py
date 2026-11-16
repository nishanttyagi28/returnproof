"""Create realistic, clearly-synthetic Indian seller data for demos and tests.

Run ``python -m returnproof.seed --reset`` for a clean local database.
Every record created here is marked ``is_sample=True`` where the schema allows
it. No marketplace API is contacted; all identifiers are invented.
"""

from __future__ import annotations

import argparse
import random
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from argon2 import PasswordHasher
from sqlalchemy import select
from sqlalchemy.orm import Session

from returnproof.config import Settings, get_settings
from returnproof.db import build_engine, build_session_factory, create_schema
from returnproof.domain.enums import (
    ClaimStatus,
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
from returnproof.domain.models import (
    ApiKey,
    AuditEvent,
    Base,
    Claim,
    Marketplace,
    Order,
    OrderItem,
    Organisation,
    Product,
    RecoveryEntry,
    ReturnRequest,
    Settlement,
    SettlementLine,
    Shipment,
    User,
    VerificationResult,
)

SAMPLE_PASSWORD = "ReturnProof@2026"
SAMPLE_PASSWORD_HASH = PasswordHasher().hash(SAMPLE_PASSWORD)

MARKETPLACES: list[dict[str, object]] = [
    {
        "code": MarketplaceCode.AMAZON_IN.value,
        "name": "Amazon.in",
        "claim_window_days": 30,
        "supports_safe_t": True,
        "config": {"template": "amazon-safe-t", "settlement_format": "amazon_mtr"},
    },
    {
        "code": MarketplaceCode.FLIPKART.value,
        "name": "Flipkart",
        "claim_window_days": 21,
        "supports_safe_t": False,
        "config": {"template": "flipkart", "settlement_format": "flipkart_payments"},
    },
    {
        "code": MarketplaceCode.MEESHO.value,
        "name": "Meesho",
        "claim_window_days": 15,
        "supports_safe_t": False,
        "config": {"template": "generic", "settlement_format": "meesho_supplier"},
    },
    {
        "code": MarketplaceCode.MYNTRA.value,
        "name": "Myntra Partner",
        "claim_window_days": 20,
        "supports_safe_t": False,
        "config": {"template": "generic", "settlement_format": "myntra_partner"},
    },
    {
        "code": MarketplaceCode.GENERIC.value,
        "name": "Other marketplace",
        "claim_window_days": 15,
        "supports_safe_t": False,
        "config": {"template": "generic", "settlement_format": "generic"},
    },
]

PRODUCTS: list[dict[str, object]] = [
    {
        "sku": "NR-AUD-001",
        "title": "boAt Airdopes 141 True Wireless Earbuds",
        "category": "Audio",
        "brand": "boAt",
        "unit_price_paise": 149900,
        "expected_weight_grams": 210,
        "requires_serial": False,
    },
    {
        "sku": "NR-PHN-014",
        "title": "Redmi 13C 5G 128 GB (Startrail Black)",
        "category": "Mobile phones",
        "brand": "Redmi",
        "unit_price_paise": 1349900,
        "expected_weight_grams": 420,
        "requires_serial": True,
    },
    {
        "sku": "NR-WCH-007",
        "title": "Noise ColorFit Pro 5 Smart Watch",
        "category": "Wearables",
        "brand": "Noise",
        "unit_price_paise": 449900,
        "expected_weight_grams": 180,
        "requires_serial": True,
    },
    {
        "sku": "NR-KTN-221",
        "title": "FabIndia Handblock Cotton Kurta (Indigo, M)",
        "category": "Apparel",
        "brand": "FabIndia",
        "unit_price_paise": 189900,
        "expected_weight_grams": 350,
        "requires_serial": False,
    },
    {
        "sku": "NR-HOM-045",
        "title": "Milton Thermosteel Flip Lid Flask 1 L",
        "category": "Home & kitchen",
        "brand": "Milton",
        "unit_price_paise": 89900,
        "expected_weight_grams": 640,
        "requires_serial": False,
    },
    {
        "sku": "NR-SPK-033",
        "title": "JBL Go 4 Portable Bluetooth Speaker",
        "category": "Audio",
        "brand": "JBL",
        "unit_price_paise": 399900,
        "expected_weight_grams": 310,
        "requires_serial": True,
    },
]

CUSTOMERS: list[tuple[str, str, str]] = [
    ("Aarav Mehta", "400001", "Maharashtra"),
    ("Diya Sharma", "110016", "Delhi"),
    ("Kabir Nair", "560034", "Karnataka"),
    ("Ishita Banerjee", "700019", "West Bengal"),
    ("Rohan Patel", "380015", "Gujarat"),
    ("Meera Iyer", "600041", "Tamil Nadu"),
    ("Arjun Singh", "302017", "Rajasthan"),
    ("Sara Khan", "500081", "Telangana"),
    ("Vivaan Gupta", "201301", "Uttar Pradesh"),
    ("Ananya Rao", "411045", "Maharashtra"),
    ("Aditya Verma", "226010", "Uttar Pradesh"),
    ("Naina Joshi", "411001", "Maharashtra"),
]

RETURN_REASONS: list[tuple[str, str, str | None]] = [
    ("damaged", "Item received damaged", "Outer box crushed, seal broken"),
    ("wrong_item", "Wrong item delivered", "Received a different colour"),
    ("size_fit", "Size or fit issue", "Kurta runs small"),
    ("not_as_described", "Not as described", "Accessories missing"),
    ("buyer_remorse", "Changed mind", "No longer needed"),
    ("empty_box", "Empty box received", "Box was sealed but empty"),
]


@dataclass
class SeedSummary:
    organisations: int = 0
    users: int = 0
    orders: int = 0
    returns: int = 0
    claims: int = 0
    settlements: int = 0
    recovery_entries: int = 0
    demo_login: tuple[str, str] = field(default=("", ""))


def _marketplaces(session: Session) -> dict[str, Marketplace]:
    existing = {m.code: m for m in session.scalars(select(Marketplace)).all()}
    for spec in MARKETPLACES:
        code = str(spec["code"])
        if code not in existing:
            marketplace = Marketplace(**spec)
            session.add(marketplace)
            existing[code] = marketplace
    session.flush()
    return existing


def _org(
    session: Session,
    *,
    name: str,
    legal_name: str,
    gstin: str,
    city: str,
    state: str,
    pincode: str,
    email: str,
) -> Organisation:
    org = Organisation(
        name=name,
        legal_name=legal_name,
        gstin=gstin,
        email=email,
        phone="+91 98200 11223",
        address_line1="Unit 402, Trade Centre",
        address_line2="Andheri East",
        city=city,
        state=state,
        pincode=pincode,
        is_sample=True,
    )
    session.add(org)
    session.flush()
    return org


def _user(
    session: Session, org: Organisation, *, email: str, name: str, role: Role
) -> User:
    user = User(
        org_id=org.id,
        email=email,
        password_hash=SAMPLE_PASSWORD_HASH,
        full_name=name,
        role=role.value,
        is_active=True,
    )
    session.add(user)
    session.flush()
    return user


def seed(session: Session, *, seed_value: int = 20261116) -> SeedSummary:
    """Populate sample data. Assumes an empty database."""
    rng = random.Random(seed_value)
    summary = SeedSummary()
    markets = _marketplaces(session)

    primary = _org(
        session,
        name="Nirvaan Retail",
        legal_name="Nirvaan Retail Private Limited",
        gstin="27AABCN4821F1Z5",
        city="Mumbai",
        state="Maharashtra",
        pincode="400069",
        email="ops@nirvaanretail.example",
    )
    secondary = _org(
        session,
        name="Kolkata Craft House",
        legal_name="Kolkata Craft House LLP",
        gstin="19AAFCK9034L1ZP",
        city="Kolkata",
        state="West Bengal",
        pincode="700091",
        email="hello@kolkatacraft.example",
    )
    owner = _user(
        session,
        primary,
        email="owner@nirvaanretail.example",
        name="Nishant Tyagi",
        role=Role.OWNER,
    )
    _user(
        session,
        primary,
        email="ops@nirvaanretail.example",
        name="Priya Deshmukh",
        role=Role.OPS,
    )
    _user(
        session,
        primary,
        email="viewer@nirvaanretail.example",
        name="Rahul Menon",
        role=Role.VIEWER,
    )
    _user(
        session,
        secondary,
        email="owner@kolkatacraft.example",
        name="Sudeshna Bose",
        role=Role.OWNER,
    )
    session.add(
        ApiKey(
            org_id=primary.id,
            created_by_id=owner.id,
            name="Warehouse scanner (sample)",
            prefix="rp_sample",
            key_hash="0" * 64,
            last_used_at=datetime.now(UTC) - timedelta(days=1),
        )
    )
    summary.organisations = 2
    summary.users = 4

    products: list[Product] = []
    for spec in PRODUCTS:
        product = Product(org_id=primary.id, is_sample=True, **spec)
        session.add(product)
        products.append(product)
    session.flush()

    now = datetime.now(UTC).replace(microsecond=0)
    order_markets = list(markets.values())[:4]
    statuses = [
        OrderStatus.DELIVERED,
        OrderStatus.DELIVERED,
        OrderStatus.DELIVERED,
        OrderStatus.RETURN_REQUESTED,
        OrderStatus.RETURNED,
    ]
    claim_templates = {
        MarketplaceCode.AMAZON_IN.value: "amazon-safe-t",
        MarketplaceCode.FLIPKART.value: "flipkart",
    }
    order_count = 36
    return_count = 0
    claim_count = 0
    for index in range(order_count):
        marketplace = order_markets[index % len(order_markets)]
        product = products[index % len(products)]
        customer, pincode, state = CUSTOMERS[index % len(CUSTOMERS)]
        placed_at = now - timedelta(days=75 - index * 2, hours=rng.randint(1, 12))
        status = statuses[index % len(statuses)]
        quantity = 1 if index % 7 else 2
        unit_price = int(product.unit_price_paise)
        total = unit_price * quantity
        order = Order(
            org_id=primary.id,
            marketplace_id=marketplace.id,
            order_number=f"{marketplace.code.upper().replace('_', '')}-{20260000 + index}",
            placed_at=placed_at,
            dispatched_at=placed_at + timedelta(hours=9),
            delivered_at=placed_at + timedelta(days=3),
            return_window_ends_at=placed_at + timedelta(days=10),
            customer_name=customer,
            customer_pincode=pincode,
            customer_state=state,
            status=status.value,
            total_paise=total,
            payment_method="cod" if index % 4 == 0 else "prepaid",
            is_sample=True,
        )
        session.add(order)
        session.flush()
        serial = None
        imei = None
        if bool(product.requires_serial):
            serial = f"SN{marketplace.code[:2].upper()}{100000 + index * 7}"
            imei = f"86{900000000000 + index * 137:012d}"
        session.add(
            OrderItem(
                order_id=order.id,
                product_id=product.id,
                sku_snapshot=product.sku,
                title_snapshot=product.title,
                quantity=quantity,
                unit_price_paise=unit_price,
                serial_number=serial,
                imei=imei,
                batch_code=f"B{placed_at:%y%m}-{index % 9 + 1}",
                expected_weight_grams=int(product.expected_weight_grams or 0) * quantity,
            )
        )
        session.add(
            Shipment(
                order_id=order.id,
                awb=f"DLV{700000000 + index * 31}",
                courier=rng.choice(["Delhivery", "Ekart", "XpressBees", "Blue Dart"]),
                dispatched_at=order.dispatched_at,
                delivered_at=order.delivered_at,
                weight_grams=int(product.expected_weight_grams or 0) * quantity,
                package_count=1,
                status=ShipmentStatus.DELIVERED.value,
            )
        )
        summary.orders += 1

        if status not in {OrderStatus.RETURN_REQUESTED, OrderStatus.RETURNED}:
            continue
        return_count += 1
        reason_code, reason_text, buyer_comment = RETURN_REASONS[index % len(RETURN_REASONS)]
        received = status == OrderStatus.RETURNED
        return_req = ReturnRequest(
            org_id=primary.id,
            order_id=order.id,
            rma_number=f"RMA-{placed_at:%y%m}-{1000 + index}",
            marketplace_return_id=f"MRET-{900000 + index}",
            requested_at=placed_at + timedelta(days=6),
            received_at=placed_at + timedelta(days=9) if received else None,
            reason_code=reason_code,
            reason_text=reason_text,
            status=ReturnStatus.RECEIVED.value if received else ReturnStatus.IN_TRANSIT.value,
            refund_amount_paise=total,
            reverse_shipping_paise=rng.choice([0, 6500, 8900]),
            buyer_comment=buyer_comment,
            is_sample=True,
        )
        session.add(return_req)
        session.flush()

        if received and index % 2 == 0:
            mismatch = index % 6 == 0
            verified = VerificationResult(
                org_id=primary.id,
                order_id=order.id,
                return_request_id=return_req.id,
                status=(
                    VerificationStatus.MISMATCH.value
                    if mismatch
                    else VerificationStatus.MATCH.value
                ),
                score=0.22 if mismatch else 0.94,
                reasons=(
                    ["Serial number on returned unit differs from dispatch record"]
                    if mismatch
                    else ["Serial number, weight and visual hash agree"]
                ),
                checklist={
                    "seal_intact": not mismatch,
                    "accessories_present": not mismatch,
                    "tags_present": True,
                    "weight_ok": not mismatch,
                },
                identifier_checks={
                    "serial_match": not mismatch,
                    "barcode_match": not mismatch,
                },
                image_distance=24 if mismatch else 3,
                verified_by_id=owner.id,
            )
            session.add(verified)
            session.flush()
            if mismatch and claim_count < 8:
                claim_count += 1
                status_cycle = [
                    ClaimStatus.DRAFT,
                    ClaimStatus.FILED,
                    ClaimStatus.APPROVED,
                    ClaimStatus.PARTIAL,
                    ClaimStatus.FILED,
                ]
                claim_status = status_cycle[claim_count % len(status_cycle)]
                filed_at = return_req.received_at + timedelta(days=1) if return_req.received_at else None
                amount = total if claim_status != ClaimStatus.PARTIAL else total // 2
                claim = Claim(
                    org_id=primary.id,
                    order_id=order.id,
                    return_request_id=return_req.id,
                    marketplace_id=marketplace.id,
                    reference=f"CLM-{placed_at:%Y%m}-{claim_count:04d}",
                    status=claim_status.value,
                    template_code=claim_templates.get(marketplace.code, "generic"),
                    amount_claimed_paise=total,
                    amount_approved_paise=amount
                    if claim_status in {ClaimStatus.APPROVED, ClaimStatus.PARTIAL}
                    else 0,
                    filed_at=filed_at,
                    deadline_at=filed_at + timedelta(days=marketplace.claim_window_days)
                    if filed_at
                    else None,
                    resolved_at=(
                        filed_at + timedelta(days=5)
                        if filed_at
                        and claim_status in {ClaimStatus.APPROVED, ClaimStatus.PARTIAL}
                        else None
                    ),
                    notes="Synthetic sample claim. Evidence pack generated for demo.",
                    is_sample=True,
                )
                session.add(claim)
                session.flush()
                summary.claims += 1
                session.add(
                    RecoveryEntry(
                        org_id=primary.id,
                        claim_id=claim.id,
                        order_id=order.id,
                        return_request_id=return_req.id,
                        marketplace_id=marketplace.id,
                        direction=RecoveryDirection.AT_RISK.value,
                        status=RecoveryStatus.OPEN.value,
                        category=LeakageCategory.REFUND_WITHOUT_RETURN.value,
                        amount_paise=total,
                        detected_at=return_req.received_at or now,
                        description="Refund processed for a returned unit that failed identity checks.",
                        is_sample=True,
                    )
                )
                summary.recovery_entries += 1
        else:
            session.add(
                RecoveryEntry(
                    org_id=primary.id,
                    order_id=order.id,
                    return_request_id=return_req.id,
                    marketplace_id=marketplace.id,
                    direction=RecoveryDirection.AT_RISK.value,
                    status=RecoveryStatus.OPEN.value,
                    category=LeakageCategory.REFUND_WITHOUT_RETURN.value,
                    amount_paise=total,
                    detected_at=return_req.requested_at,
                    description="Refund issued while the return is still in transit.",
                    is_sample=True,
                )
            )
            summary.recovery_entries += 1
    summary.returns = return_count

    settlement_specs = [
        (markets[MarketplaceCode.AMAZON_IN.value], 22, 14),
        (markets[MarketplaceCode.FLIPKART.value], 10, 26),
    ]
    for marketplace, days_ago_start, days_ago_end in settlement_specs:
        start = (now - timedelta(days=days_ago_start)).replace(hour=0, minute=0, second=0)
        end = (now - timedelta(days=days_ago_end)).replace(hour=23, minute=59, second=59)
        settlement = Settlement(
            org_id=primary.id,
            marketplace_id=marketplace.id,
            period_start=start,
            period_end=end,
            file_name=f"{marketplace.code}_settlement_sample.csv",
            file_sha256="a" * 64,
            row_count=6,
            total_net_paise=0,
            status="imported",
            is_sample=True,
        )
        session.add(settlement)
        session.flush()
        order_rows = session.scalars(
            select(Order)
            .where(Order.org_id == primary.id, Order.marketplace_id == marketplace.id)
            .order_by(Order.placed_at.desc())
            .limit(6)
        ).all()
        total_net = 0
        for row_index, order in enumerate(order_rows):
            gross = order.total_paise
            commission = int(gross * 0.12)
            shipping = 7200
            reverse = 0
            refund = 0
            txn = "order_payment"
            if row_index == 1:
                txn = "refund"
                refund = gross
                reverse = 8900
            net = gross - commission - shipping - reverse - refund
            total_net += net
            line = SettlementLine(
                settlement_id=settlement.id,
                order_number=order.order_number,
                transaction_type=txn,
                marketplace_sku="NR-SAMPLE",
                quantity=1,
                gross_paise=gross,
                commission_paise=commission,
                shipping_paise=shipping,
                reverse_shipping_paise=reverse,
                refund_paise=refund,
                other_fee_paise=0,
                net_paise=net,
                raw={"sample": True, "order": order.order_number},
            )
            session.add(line)
            if refund and reverse:
                session.flush()
                session.add(
                    RecoveryEntry(
                        org_id=primary.id,
                        marketplace_id=marketplace.id,
                        order_id=order.id,
                        settlement_line_id=line.id,
                        direction=RecoveryDirection.AT_RISK.value,
                        status=RecoveryStatus.OPEN.value,
                        category=LeakageCategory.REVERSE_SHIPPING_ERROR.value,
                        amount_paise=reverse,
                        detected_at=end,
                        description="Reverse shipping charged on a return that was never received.",
                        is_sample=True,
                    )
                )
                summary.recovery_entries += 1
        settlement.total_net_paise = total_net
        summary.settlements += 1

    session.add_all(
        [
            AuditEvent(
                org_id=primary.id,
                actor_user_id=owner.id,
                entity_type="claim",
                entity_id="1",
                action="claim.filed",
                summary="Sample claim filed with Amazon.in",
                data={"sample": True},
            ),
            AuditEvent(
                org_id=primary.id,
                actor_user_id=owner.id,
                entity_type="settlement",
                entity_id="1",
                action="settlement.imported",
                summary="Sample Amazon MTR settlement imported",
                data={"sample": True},
            ),
        ]
    )
    session.flush()
    summary.demo_login = ("owner@nirvaanretail.example", SAMPLE_PASSWORD)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed ReturnProof with synthetic sample data.")
    parser.add_argument("--reset", action="store_true", help="Drop and recreate all tables.")
    parser.add_argument("--database-url", default=None, help="Override configured database URL.")
    args = parser.parse_args()

    settings: Settings = get_settings()
    database_url = args.database_url or settings.database_url
    engine = build_engine(database_url)
    if args.reset:
        Base.metadata.drop_all(engine)
    create_schema(engine)
    session = build_session_factory(engine)()
    try:
        existing = session.scalar(select(Organisation.id).limit(1))
        if existing and not args.reset:
            print("Database already contains data. Re-run with --reset for a clean sample set.")
            return
        summary = seed(session)
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
        engine.dispose()

    print("ReturnProof sample data seeded (synthetic, not real seller data).")
    print(f"  organisations : {summary.organisations}")
    print(f"  users         : {summary.users}")
    print(f"  orders        : {summary.orders}")
    print(f"  returns       : {summary.returns}")
    print(f"  claims        : {summary.claims}")
    print(f"  settlements   : {summary.settlements}")
    print(f"  recovery rows : {summary.recovery_entries}")
    print(f"  demo login    : {summary.demo_login[0]} / {summary.demo_login[1]}")
    print(f"  database      : {Path(database_url).name if database_url.startswith('sqlite') else database_url}")


if __name__ == "__main__":
    main()
