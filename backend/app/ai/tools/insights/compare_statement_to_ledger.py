"""compare_statement_to_ledger - a reconciliation view between an uploaded
bank statement's extracted rows and the user's actual ledger, over the same
period.

A DAY-LEVEL TOTALS DIFF, not a row-by-row match: statement rows have no
natural join key to a ledger_transactions row (statements/statement_rows are
a completely separate table, never linked to journal_transactions - see
app/modules/statements/service.py's module docstring), so "does this
differ" is answered at the aggregate level the model can narrate sensibly,
not by trying to pair up individual rows a bank's PDF layout gives no way to
match reliably.

Like read_document, this tool takes NO arguments - the statement compared
is context.statement_id, never model-chosen (see document_tools.py's module
docstring for the same pattern and reasoning).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime, time
from typing import TYPE_CHECKING

from pydantic import BaseModel

from app.ai.context import Context
from app.ai.schemas import ToolResult
from app.ai.tools.base import Tool
from app.core.exceptions import NotFoundError

if TYPE_CHECKING:
    from supabase import AsyncClient


class CompareStatementToLedgerInput(BaseModel):
    """Deliberately empty - see the module docstring."""


def _as_date(value: object) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


class CompareStatementToLedgerTool(Tool):
    name = "compare_statement_to_ledger"
    description = (
        "Compară rândurile extrase din extrasul de cont activ cu tranzacțiile "
        "reale din jurnalul contabil, pentru aceeași perioadă. Întoarce "
        "totaluri de intrări/ieșiri din ambele surse, diferența dintre ele, și "
        "o listă a zilelor unde cele două nu se potrivesc. NU acceptă "
        "argumente - folosește întotdeauna extrasul activ al conversației."
    )
    input_schema = CompareStatementToLedgerInput
    read_only = True

    def __init__(self, supabase: AsyncClient) -> None:
        self._supabase = supabase

    async def run(self, validated_input: BaseModel, context: Context) -> ToolResult:
        assert isinstance(validated_input, CompareStatementToLedgerInput)
        del validated_input

        if not context.statement_id:
            return ToolResult.failure(
                name=self.name,
                error="Nu există niciun extras activ în această conversație.",
            )

        from app.modules.statements import service as statements_service
        from app.modules.transactions import service as transactions_service

        try:
            statement = await statements_service.get_statement_with_rows(
                self._supabase, context.user_id, context.statement_id
            )
        except NotFoundError:
            return ToolResult.failure(
                name=self.name,
                error="Extrasul activ nu mai este disponibil.",
            )

        rows = statement["rows"]
        dated_rows = [r for r in rows if r.get("posted_date")]
        if not dated_rows:
            return ToolResult(
                name=self.name,
                data={
                    "period": None,
                    "statement_totals": {"in_minor": 0, "out_minor": 0},
                    "ledger_totals": {"in_minor": 0, "out_minor": 0},
                    "difference": {"in_minor": 0, "out_minor": 0},
                    "daily_mismatches": [],
                    "note": "Extrasul nu are rânduri cu dată - nimic de comparat.",
                },
            )

        period_start = statement.get("period_start") or min(
            r["posted_date"] for r in dated_rows
        )
        period_end = statement.get("period_end") or max(r["posted_date"] for r in dated_rows)
        date_from = datetime.combine(_as_date(period_start), time.min, tzinfo=UTC)
        date_to = datetime.combine(_as_date(period_end), time.max, tzinfo=UTC)

        ledger_entries = await transactions_service.list_user_transactions_in_range_for_owner(
            self._supabase,
            context.user_id,
            date_from=date_from,
            date_to=date_to,
            limit=transactions_service.ANALYTICS_MAX_LIMIT,
        )

        statement_by_day: dict[str, dict[str, int]] = defaultdict(
            lambda: {"in_minor": 0, "out_minor": 0}
        )
        for row in dated_rows:
            day = str(row["posted_date"])
            amount = float(row["amount"])
            amount_minor = round(abs(amount) * 100)
            if amount >= 0:
                statement_by_day[day]["in_minor"] += amount_minor
            else:
                statement_by_day[day]["out_minor"] += amount_minor

        ledger_by_day: dict[str, dict[str, int]] = defaultdict(
            lambda: {"in_minor": 0, "out_minor": 0}
        )
        for entry in ledger_entries:
            day = entry.created_at.date().isoformat()
            if entry.direction.value == "credit":
                ledger_by_day[day]["in_minor"] += entry.amount_minor
            else:
                ledger_by_day[day]["out_minor"] += entry.amount_minor

        all_days = sorted(set(statement_by_day) | set(ledger_by_day))
        daily_mismatches = []
        for day in all_days:
            s = statement_by_day.get(day, {"in_minor": 0, "out_minor": 0})
            ledger_day = ledger_by_day.get(day, {"in_minor": 0, "out_minor": 0})
            if s["in_minor"] != ledger_day["in_minor"] or s["out_minor"] != ledger_day["out_minor"]:
                daily_mismatches.append(
                    {
                        "date": day,
                        "statement_in_minor": s["in_minor"],
                        "statement_out_minor": s["out_minor"],
                        "ledger_in_minor": ledger_day["in_minor"],
                        "ledger_out_minor": ledger_day["out_minor"],
                    }
                )

        statement_totals = {
            "in_minor": sum(d["in_minor"] for d in statement_by_day.values()),
            "out_minor": sum(d["out_minor"] for d in statement_by_day.values()),
        }
        ledger_totals = {
            "in_minor": sum(d["in_minor"] for d in ledger_by_day.values()),
            "out_minor": sum(d["out_minor"] for d in ledger_by_day.values()),
        }

        return ToolResult(
            name=self.name,
            data={
                "period": {"start": str(period_start), "end": str(period_end)},
                "statement_totals": statement_totals,
                "ledger_totals": ledger_totals,
                "difference": {
                    "in_minor": statement_totals["in_minor"] - ledger_totals["in_minor"],
                    "out_minor": statement_totals["out_minor"] - ledger_totals["out_minor"],
                },
                "daily_mismatches": daily_mismatches,
            },
        )
