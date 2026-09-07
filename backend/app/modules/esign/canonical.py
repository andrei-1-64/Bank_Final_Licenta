"""The exact byte sequence that gets signed and later re-verified.

Deliberately NOT `json.dumps(...).encode()`: JSON key order, whitespace and
number formatting are not guaranteed stable across Python versions or
libraries, so two "equal" dicts can serialize to different bytes - which
would make a signature that was valid at signing time fail to re-verify
later for no real reason. A fixed, pipe-delimited field order removes that
whole failure mode.

CANONICAL_VERSION is embedded in the payload itself (not just implied by
which code produced it) so that if this format ever changes, an old
signature still names the version it was made under instead of silently
being re-interpreted under new rules.
"""

from __future__ import annotations

CANONICAL_VERSION = "v1"


def build_canonical_payload(
    *,
    proposal_id: str,
    document_sha256: str,
    user_id: str,
    signed_at_iso: str,
    auth_method: str,
    intent: str,
) -> bytes:
    fields = (
        CANONICAL_VERSION,
        proposal_id,
        document_sha256,
        user_id,
        signed_at_iso,
        auth_method,
        intent,
    )
    return "|".join(fields).encode("utf-8")
