"""
Mint a service token for local testing.

This is the DEV-ONLY stand-in for what the Laravel backend will do in
production (see docs/handover-backend.md §3). It reads SERVICE_JWT_SECRET
from .env so the token it prints is always valid for the running service.

    python scripts/dev_token.py                       # defaults
    python scripts/dev_token.py --user 42 --session s1
    python scripts/dev_token.py --curl                # ready-to-paste commands
    python scripts/dev_token.py --expired             # negative test
    python scripts/dev_token.py --no-exp              # negative test
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import jwt  # noqa: E402

from app.core.config import settings  # noqa: E402


def mint(user_id: str, session_id: str, *, expires_in: int | None, secret: str) -> str:
    now = datetime.now(UTC)
    payload: dict = {
        "sub": user_id,
        "session_id": session_id,
        "timezone": "America/New_York",
        "currency": "USD",
        "iat": now,
    }
    if expires_in is not None:
        payload["exp"] = now + timedelta(seconds=expires_in)
    return jwt.encode(payload, secret, algorithm=settings.service_jwt_algorithm)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--user", default="42", help="value for the `sub` claim (application_users.id)")
    p.add_argument("--session", default="session-alpha", help="conversation id")
    p.add_argument("--host", default="http://127.0.0.1:8000")
    p.add_argument(
        "--ttl",
        type=int,
        default=300,
        help="token lifetime in seconds (service caps this at "
             "SERVICE_JWT_MAX_LIFETIME_SECONDS, default 900)",
    )
    p.add_argument("--curl", action="store_true", help="print ready-to-run curl commands")
    p.add_argument("--expired", action="store_true", help="mint a token that expired an hour ago")
    p.add_argument("--no-exp", action="store_true", help="mint a token with no exp claim")
    p.add_argument("--wrong-secret", action="store_true", help="sign with the wrong secret")
    args = p.parse_args()

    if not settings.service_jwt_secret:
        print("SERVICE_JWT_SECRET is empty — set it in .env first.", file=sys.stderr)
        return 1

    secret = "x" * 40 if args.wrong_secret else settings.service_jwt_secret
    expires_in = None if args.no_exp else (-3600 if args.expired else args.ttl)

    token = mint(args.user, args.session, expires_in=expires_in, secret=secret)

    if not args.curl:
        print(token)
        return 0

    body = f'{{"session_id": "{args.session}", "message": "How much did I spend last month?"}}'
    print(f"""# --- health (no auth) ---
curl {args.host}/health

# --- create a conversation ---
curl -X POST {args.host}/api/v1/conversations \\
  -H "Authorization: Bearer {token}" \\
  -H "X-Session-Id: {args.session}"

# --- chat (needs OPENAI_API_KEY set) ---
curl -X POST {args.host}/api/v1/chat \\
  -H "Authorization: Bearer {token}" \\
  -H "X-Session-Id: {args.session}" \\
  -H "Content-Type: application/json" \\
  -d '{body}'

# --- read the conversation back ---
curl {args.host}/api/v1/conversations/{args.session} \\
  -H "Authorization: Bearer {token}" \\
  -H "X-Session-Id: {args.session}"
""")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
