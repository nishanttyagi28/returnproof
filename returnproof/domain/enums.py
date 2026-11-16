"""Shared enumerations for the ReturnProof domain."""

from __future__ import annotations

from enum import StrEnum


class Role(StrEnum):
    OWNER = "owner"
    OPS = "ops"
    VIEWER = "viewer"


class MarketplaceCode(StrEnum):
    AMAZON_IN = "amazon_in"
    FLIPKART = "flipkart"
    MEESHO = "meesho"
    MYNTRA = "myntra"
    GENERIC = "generic"


class OrderStatus(StrEnum):
    PENDING = "pending"
    DISPATCHED = "dispatched"
    DELIVERED = "delivered"
    RETURN_REQUESTED = "return_requested"
    RETURNED = "returned"
    CANCELLED = "cancelled"


class ShipmentStatus(StrEnum):
    CREATED = "created"
    IN_TRANSIT = "in_transit"
    DELIVERED = "delivered"
    RTO = "rto"
    LOST = "lost"


class ReturnStatus(StrEnum):
    REQUESTED = "requested"
    IN_TRANSIT = "in_transit"
    RECEIVED = "received"
    VERIFIED = "verified"
    CLOSED = "closed"
    REJECTED = "rejected"
    NOT_RETURNED = "not_returned"


class EvidenceStage(StrEnum):
    DISPATCH = "dispatch"
    RETURN_RECEIPT = "return_receipt"


class EvidenceKind(StrEnum):
    PHOTO = "photo"
    VIDEO = "video"
    DOCUMENT = "document"


class VerificationStatus(StrEnum):
    MATCH = "match"
    MISMATCH = "mismatch"
    NEEDS_REVIEW = "needs_review"


class ClaimStatus(StrEnum):
    DRAFT = "draft"
    FILED = "filed"
    APPROVED = "approved"
    REJECTED = "rejected"
    PARTIAL = "partial"


class RecoveryDirection(StrEnum):
    AT_RISK = "at_risk"
    RECOVERED = "recovered"


class RecoveryStatus(StrEnum):
    OPEN = "open"
    RECOVERED = "recovered"
    WRITTEN_OFF = "written_off"


class LeakageCategory(StrEnum):
    REFUND_WITHOUT_RETURN = "refund_without_return"
    REVERSE_SHIPPING_ERROR = "reverse_shipping_error"
    APPROVED_NOT_PAID = "approved_not_paid"
    COMMISSION_MISMATCH = "commission_mismatch"
    FEE_MISMATCH = "fee_mismatch"
    CLAIM_PAYOUT = "claim_payout"
    OTHER = "other"
