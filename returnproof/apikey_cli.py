"""Create an API key from the command line (plaintext shown once).

Usage:
    python -m returnproof.apikey_cli --email owner@nirvaanretail.example --name warehouse
"""

from __future__ import annotations

import argparse

from sqlalchemy import select

from returnproof.auth import generate_api_key
from returnproof.config import get_settings
from returnproof.db import build_engine, build_session_factory
from returnproof.domain.models import ApiKey, User


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a ReturnProof API key.")
    parser.add_argument("--email", required=True, help="User email inside the organisation")
    parser.add_argument("--name", default="cli key", help="Label for the key")
    parser.add_argument("--database-url", default=None)
    args = parser.parse_args()

    settings = get_settings()
    engine = build_engine(args.database_url or settings.database_url)
    db = build_session_factory(engine)()
    try:
        user = db.scalar(
            select(User).where(User.email == args.email.strip().lower()).order_by(User.id)
        )
        if user is None:
            raise SystemExit(f"No user found for {args.email}")
        token, prefix, key_hash = generate_api_key()
        key = ApiKey(
            org_id=user.org_id,
            created_by_id=user.id,
            name=args.name,
            prefix=prefix,
            key_hash=key_hash,
        )
        db.add(key)
        db.commit()
        print(f"API key created for {user.email} ({user.role})")
        print(f"  name:  {key.name}")
        print(f"  token: {token}")
        print("Store this token now; only its SHA-256 hash is persisted.")
    finally:
        db.close()
        engine.dispose()


if __name__ == "__main__":
    main()
