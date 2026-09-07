"""The Ed25519 signing key. Loaded once from ESIGN_PRIVATE_KEY (an env var,
never the database) and cached for the life of the process.

Deliberately NOT a key-management service: there is one active key, named by
ESIGN_KEY_ID. Rotating it means setting a new ESIGN_PRIVATE_KEY/ESIGN_KEY_ID
pair and redeploying - `ensure_key_registered` then adds a new row to
`signing_keys` alongside the old one, which is never touched. A signature
made under the old key stays verifiable forever: verification only needs the
PUBLIC key recorded for whatever key_id the signature names, never the
currently-active private key.
"""

from __future__ import annotations

import base64

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from postgrest.exceptions import APIError
from supabase import AsyncClient

from app.config import ConfigurationError, settings
from app.core.exceptions import ESignUnavailableError
from app.db.supabase_client import UNIQUE_VIOLATION

_private_key: Ed25519PrivateKey | None = None


def _load_private_key() -> Ed25519PrivateKey:
    global _private_key
    if _private_key is not None:
        return _private_key

    try:
        raw_value = settings.require_esign()
    except ConfigurationError as exc:
        raise ESignUnavailableError() from exc

    try:
        seed = base64.b64decode(raw_value, validate=True)
    except Exception as exc:
        raise ESignUnavailableError(
            "ESIGN_PRIVATE_KEY is not valid base64."
        ) from exc
    if len(seed) != 32:
        raise ESignUnavailableError(
            "ESIGN_PRIVATE_KEY must decode to exactly 32 bytes (an Ed25519 seed)."
        )

    _private_key = Ed25519PrivateKey.from_private_bytes(seed)
    return _private_key


def key_id() -> str:
    return settings.ESIGN_KEY_ID


def sign(payload: bytes) -> bytes:
    return _load_private_key().sign(payload)


def public_key_b64() -> str:
    public_bytes = _load_private_key().public_key().public_bytes_raw()
    return base64.b64encode(public_bytes).decode()


def verify(public_key_b64_value: str, payload: bytes, signature: bytes) -> bool:
    """Checked against a STORED public key (signing_keys.public_key_b64),
    not necessarily the currently-active one - this is what lets a
    signature made under a rotated-out key still verify."""
    try:
        public_key = Ed25519PublicKey.from_public_bytes(
            base64.b64decode(public_key_b64_value)
        )
        public_key.verify(signature, payload)
        return True
    except (InvalidSignature, ValueError):
        return False


async def ensure_key_registered(supabase: AsyncClient) -> None:
    """Idempotent: upserts the active key's public half into `signing_keys`
    on first use. Never updates an existing row - see the module docstring
    on why a key_id's public key must never change once recorded."""
    existing = (
        await supabase.table("signing_keys")
        .select("key_id")
        .eq("key_id", key_id())
        .maybe_single()
        .execute()
    )
    if existing is not None and existing.data:
        return

    try:
        await (
            supabase.table("signing_keys")
            .insert(
                {
                    "key_id": key_id(),
                    "algorithm": "ed25519",
                    "public_key_b64": public_key_b64(),
                }
            )
            .execute()
        )
    except APIError as exc:
        # Lost a race with another request registering the same key_id at
        # the same time - the row exists either way, which is all this
        # function promises.
        if exc.code != UNIQUE_VIOLATION:
            raise
