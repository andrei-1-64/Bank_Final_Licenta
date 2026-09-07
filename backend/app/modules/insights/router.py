"""REST surface for the insights tools that also back the AI chat agent (see
app/ai/tools/insights/). Kept separate from the chat tool-call loop: a
dashboard widget rendering on page load shouldn't need an LLM round-trip
just to categorize this month's spending.
"""

from datetime import date

from fastapi import APIRouter, Depends, Query
from supabase import AsyncClient

from app.ai.context import build_context_for_user
from app.ai.tools.insights.categorize_transactions import categorize_spending
from app.core.dependencies import get_current_user
from app.core.exceptions import ValidationError
from app.db.supabase_client import get_supabase
from app.modules.users.schemas import UserRead

router = APIRouter()


@router.get("/spending-by-category")
async def get_spending_by_category(
    start_date: date = Query(...),
    end_date: date = Query(...),
    account_id: str | None = Query(default=None),
    supabase: AsyncClient = Depends(get_supabase),
    user: UserRead = Depends(get_current_user),
) -> dict:
    if end_date < start_date:
        raise ValidationError("end_date must be on or after start_date")
    context = await build_context_for_user(user, supabase)
    return await categorize_spending(
        supabase,
        context,
        start_date=start_date,
        end_date=end_date,
        account_id=account_id,
    )
