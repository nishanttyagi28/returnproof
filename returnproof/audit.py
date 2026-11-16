"""Append-only audit events for state changes."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from returnproof.domain.models import AuditEvent


def record_audit(
    db: Session,
    *,
    org_id: int,
    actor_user_id: int | None,
    entity_type: str,
    entity_id: str | int,
    action: str,
    summary: str,
    data: dict[str, Any] | None = None,
    ip_address: str | None = None,
) -> AuditEvent:
    event = AuditEvent(
        org_id=org_id,
        actor_user_id=actor_user_id,
        entity_type=entity_type,
        entity_id=str(entity_id),
        action=action,
        summary=summary[:400],
        data=data or {},
        ip_address=ip_address,
    )
    db.add(event)
    return event


def audit_and_commit(
    db: Session,
    *,
    org_id: int,
    actor_user_id: int | None,
    entity_type: str,
    entity_id: str | int,
    action: str,
    summary: str,
    data: dict[str, Any] | None = None,
    ip_address: str | None = None,
) -> AuditEvent:
    event = record_audit(
        db,
        org_id=org_id,
        actor_user_id=actor_user_id,
        entity_type=entity_type,
        entity_id=entity_id,
        action=action,
        summary=summary,
        data=data,
        ip_address=ip_address,
    )
    db.commit()
    db.refresh(event)
    return event
