"""Password hashing + server-side session token primitives.

These are the shared building blocks. This file does NOT expose any HTTP
endpoints — the [A]-owned core/dependencies.py::get_current_user is the
*consumer* of sessions created with these helpers, and the auth teammate's
login/logout/register endpoints are the *producer*. See
docs/AUTH_HANDOFF.md for the exact contract.

Session design: the cookie carries a single opaque, high-entropy random
token (generate_session_token). We never store that raw token — only its
SHA-256 hash (hash_session_token) goes in sessions.token_hash. This is the
same "store a hash, not the secret" pattern as passwords, but SHA-256 (not
bcrypt) is appropriate here because the token itself already has 256 bits of
entropy, so there's nothing for a slow KDF to protect against.
"""

import hashlib
import secrets

import bcrypt

SESSION_TOKEN_BYTES = 32


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        # Malformed/foreign hash format - never let this crash the request.
        return False


def generate_session_token() -> str:
    """Raw token to put in the cookie. Returned to the client exactly once
    (at login time) - it is never persisted, only its hash is."""
    return secrets.token_urlsafe(SESSION_TOKEN_BYTES)


def hash_session_token(token: str) -> str:
    """Deterministic hash used both to store and to look up sessions."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_otp_code() -> str:
    """A random 6-digit password-reset code, zero-padded (e.g. "004821")."""
    return f"{secrets.randbelow(1_000_000):06d}"


_REFERRAL_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I - avoids visual ambiguity when shared
_REFERRAL_CODE_LENGTH = 8


def generate_referral_code() -> str:
    """A random per-user shareable code (e.g. "K7M2XQP9"). Collisions are
    handled by the caller (retry on the DB's UNIQUE violation), same pattern
    as accounts/service.py's IBAN generation."""
    return "".join(secrets.choice(_REFERRAL_CODE_ALPHABET) for _ in range(_REFERRAL_CODE_LENGTH))


def hash_otp_code(code: str) -> str:
    """Same store-a-hash-not-the-secret pattern as hash_session_token. Unlike
    a session token, a 6-digit code has only 1e6 possibilities, so this is
    hygiene (a DB dump doesn't hand out usable codes), not real brute-force
    resistance - expires_at + attempts on password_reset_codes do that job."""
    return hashlib.sha256(code.encode("utf-8")).hexdigest()
