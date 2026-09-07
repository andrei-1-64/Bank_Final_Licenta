"""Append-only audit log. Rows are never updated or deleted.

Writes here are each their own independent REST call - unlike the old
SQLAlchemy version, they no longer share a transaction with the business
write they document (accepted, documented limitation of the Supabase REST
migration; see backend/supabase/migrations/0002_ledger_functions.sql's
header for the one exception: the ledger's own audit insert happens inside
the post_transaction/create_transfer Postgres functions, so it stays atomic
with the money movement).
"""

import uuid
from typing import Any

from supabase import AsyncClient


async def record_audit_event(
    supabase: AsyncClient,
    *,
    user_id: uuid.UUID | None,
    action: str,
    entity: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    await supabase.table("audit_log").insert(
        {
            "user_id": str(user_id) if user_id else None,
            "action": action,
            "entity": entity,
            "metadata_json": metadata or {},
        }
    ).execute()
