"""Password, session, CSRF and API-key security primitives."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from returnproof.db import get_db
from returnproof.domain.enums import Role
from returnproof.domain.models import ApiKey, User, utcnow

_hasher = PasswordHasher()
API_KEY_NAMESPACE = "rp"
API_KEY_PREFIX_LEN = 12


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def password_needs_rehash(password_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return True


def generate_api_key() -> tuple[str, str, str]:
    """Return ``(plaintext, prefix, sha256_hash)``. Plaintext is shown only once."""
    token = f"{API_KEY_NAMESPACE}_{secrets.token_hex(24)}"
    return token, token[:API_KEY_PREFIX_LEN], hash_api_key(token)


def hash_api_key(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def csrf_token(request: Request) -> str:
    token = request.session.get("csrf")
    if not isinstance(token, str) or len(token) < 16:
        token = secrets.token_urlsafe(24)
        request.session["csrf"] = token
    return token


def validate_csrf(request: Request, submitted: str | None) -> None:
    expected = request.session.get("csrf")
    if not isinstance(expected, str) or not submitted or not hmac.compare_digest(expected, submitted):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token")


def login_session(request: Request, user: User) -> None:
    """Establish a fresh authenticated session, rotating the CSRF token."""
    request.session.clear()
    request.session.update(
        {
            "uid": user.id,
            "org": user.org_id,
            "role": user.role,
            "csrf": secrets.token_urlsafe(24),
            "auth_at": int(datetime.now(UTC).timestamp()),
        }
    )


def logout_session(request: Request) -> None:
    request.session.clear()


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    uid = request.session.get("uid")
    org_id = request.session.get("org")
    if not isinstance(uid, int) or not isinstance(org_id, int):
        return None
    user = db.get(User, uid)
    if (
        user is None
        or not user.is_active
        or user.org_id != org_id
        or user.role != request.session.get("role")
    ):
        request.session.clear()
        return None
    return user


def require_user(user: User | None = Depends(get_current_user)) -> User:
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": "/login"},
            detail="Authentication required",
        )
    return user


def require_roles(*allowed: Role) -> object:
    allowed_values = {role.value for role in allowed}

    def dependency(user: User = Depends(require_user)) -> User:
        if user.role not in allowed_values:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Your role cannot perform this action",
            )
        return user

    return dependency


@dataclass(frozen=True)
class Principal:
    org_id: int
    role: str
    user_id: int | None
    api_key_id: int | None
    auth_method: str


def _extract_token(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        token = header[7:].strip()
        return token or None
    key = request.headers.get("x-api-key")
    return key.strip() if key else None


def authenticate_api_key(request: Request, db: Session = Depends(get_db)) -> Principal:
    token = _extract_token(request)
    if not token or not token.startswith(f"{API_KEY_NAMESPACE}_"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API key required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    prefix = token[:API_KEY_PREFIX_LEN]
    api_key = db.scalar(select(ApiKey).where(ApiKey.prefix == prefix))
    if api_key is None or api_key.revoked_at is not None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or revoked API key",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not hmac.compare_digest(api_key.key_hash, hash_api_key(token)):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
            headers={"WWW-Authenticate": "Bearer"},
        )
    role = Role.OWNER.value
    if api_key.created_by_id is not None:
        creator = db.get(User, api_key.created_by_id)
        if creator is not None and creator.org_id == api_key.org_id:
            role = creator.role
    api_key.last_used_at = utcnow()
    db.commit()
    return Principal(
        org_id=api_key.org_id,
        role=role,
        user_id=api_key.created_by_id,
        api_key_id=api_key.id,
        auth_method="api_key",
    )


def require_api_roles(*allowed: Role) -> object:
    allowed_values = {role.value for role in allowed}

    def dependency(principal: Principal = Depends(authenticate_api_key)) -> Principal:
        if principal.role not in allowed_values:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="API key role cannot perform this action",
            )
        return principal

    return dependency


def purge_expired_session_marker(request: Request, max_age: timedelta) -> bool:
    """Return True and clear the session when the auth marker is stale."""
    auth_at = request.session.get("auth_at")
    if not isinstance(auth_at, int):
        return False
    if datetime.now(UTC) - datetime.fromtimestamp(auth_at, UTC) > max_age:
        request.session.clear()
        return True
    return False
