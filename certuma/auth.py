"""Dashboard auth + RBAC (Phase 3 task P3.9).

Stdlib-only: salted PBKDF2-HMAC-SHA256 password hashing and an HMAC-SHA256-signed, expiring session
token carried in a cookie. Roles: operator and admin can mutate; leadership is READ-ONLY (the
proposal's role-based access + leadership visibility). The session secret is injected (never
hardcoded); the app sources it from CERTUMA_SESSION_SECRET. Verification is constant-time
(hmac.compare_digest) and a forged or expired token simply fails closed.

PBKDF2 (200k iterations) is a reasonable stdlib choice for an internal tool; swap in argon2/bcrypt
behind hash_password/verify_password if this ever faces the public internet.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time
from typing import TYPE_CHECKING, Optional, Tuple

if TYPE_CHECKING:  # the DB helpers import SQLAlchemy lazily; hashing/session stay pure-importable
    from sqlalchemy.orm import Session

    from certuma.db.models import ConsoleUser

__all__ = [
    "ROLES", "WRITE_ROLES", "ADMIN_ROLES", "SESSION_COOKIE", "SESSION_TTL",
    "hash_password", "verify_password", "create_user", "authenticate",
    "sign_session", "verify_session", "can_write", "is_admin",
]

ROLES = ("operator", "leadership", "admin")
WRITE_ROLES = frozenset({"operator", "admin"})   # leadership is read-only
ADMIN_ROLES = frozenset({"admin"})
SESSION_COOKIE = "certuma_session"
SESSION_TTL = 8 * 3600  # 8 hours
_ITERATIONS = 200_000


def hash_password(password: str, salt: Optional[str] = None) -> Tuple[str, str]:
    """Return (hash_hex, salt_hex). A new random salt is generated when none is supplied."""
    salt_b = bytes.fromhex(salt) if salt else os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt_b, _ITERATIONS)
    return digest.hex(), salt_b.hex()


def verify_password(password: str, password_hash: str, salt: str) -> bool:
    calc, _ = hash_password(password, salt)
    return hmac.compare_digest(calc, password_hash)


def create_user(session: "Session", *, username: str, password: str, role: str) -> "ConsoleUser":
    from certuma.db.models import ConsoleUser
    if role not in ROLES:
        raise ValueError(f"unknown role {role!r}")
    if not username.strip() or not password:
        raise ValueError("username and password are required")
    h, s = hash_password(password)
    user = ConsoleUser(username=username.strip(), password_hash=h, salt=s, role=role, is_active=True)
    session.add(user)
    session.flush()
    return user


def authenticate(session: "Session", *, username: str, password: str) -> Optional["ConsoleUser"]:
    """Return the active user iff the password matches, else None. Fails closed; equalizes timing."""
    from sqlalchemy import select

    from certuma.db.models import ConsoleUser
    user = session.execute(
        select(ConsoleUser).where(ConsoleUser.username == username, ConsoleUser.is_active.is_(True))
    ).scalar()
    if user is None:
        hash_password(password)  # do the work anyway so a missing user is not obviously faster
        return None
    return user if verify_password(password, user.password_hash, user.salt) else None


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode("ascii").rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def sign_session(user_id: int, role: str, *, secret: str, ttl: int = SESSION_TTL,
                 now: Optional[int] = None) -> str:
    now = now if now is not None else int(time.time())
    payload = f"{user_id}:{role}:{now + ttl}".encode("ascii")
    sig = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).digest()
    return _b64(payload) + "." + _b64(sig)


def verify_session(token: str, *, secret: str, now: Optional[int] = None) -> Optional[dict]:
    """Return {'user_id', 'role'} for a valid, unexpired, untampered token, else None."""
    now = now if now is not None else int(time.time())
    try:
        payload_b64, sig_b64 = token.split(".", 1)
        payload = _unb64(payload_b64)
        sig = _unb64(sig_b64)
    except Exception:
        return None
    expected = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        user_id_s, role, exp_s = payload.decode("ascii").split(":")
        if int(exp_s) < now:
            return None
        if role not in ROLES:
            return None
    except Exception:
        return None
    return {"user_id": int(user_id_s), "role": role}


def can_write(role: Optional[str]) -> bool:
    return role in WRITE_ROLES


def is_admin(role: Optional[str]) -> bool:
    return role in ADMIN_ROLES
