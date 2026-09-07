"""statement_tools - DocumentAgent's second tool (Step 13): summarize_statement.

Added alongside read_document under the SAME structural invariant described
in document_tools.py's module docstring: no write tools, no handoff to any
other agent (see AIService.build_document_tools) - a statement's extracted
text is UNTRUSTED input exactly like a document's, and that isolation is
still what makes it safe, not a prompt promise.

`summarize_statement` is aggregate-only: it NEVER puts raw per-row
descriptions into the model-facing result, only computed totals plus the
statement's own metadata (bank_name, period, currency) - the row-level text
a malicious PDF could plant something in never reaches the prompt through
this tool. `wrap_statement_content` wraps the one piece of free text that
does reach it (bank_name), mirroring document_tools.py's
<untrusted_document> pattern exactly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel

from app.ai.context import Context
from app.ai.schemas import ToolResult
from app.ai.tools.base import Tool
from app.core.exceptions import NotFoundError

if TYPE_CHECKING:
    from supabase import AsyncClient


def wrap_statement_content(text: str) -> str:
    """Wrap free text sourced from an extracted statement before it reaches
    a prompt - the statement equivalent of document_tools.py's
    <untrusted_document> wrapping. Same reasoning: structural isolation
    (DocumentAgent has no write tools, no handoff) is the actual security
    boundary; this is defense in depth on top of it, telling the model the
    wrapped span is DATA, not instructions (see document_agent.py's
    SYSTEM_PROMPT, which spells that rule out for the model)."""
    return f"<untrusted_statement>{text}</untrusted_statement>"


class SummarizeStatementInput(BaseModel):
    """Deliberately empty - see document_tools.py's ReadDocumentInput for
    why: the statement summarized is context.statement_id, never
    model-chosen."""


class SummarizeStatementTool(Tool):
    name = "summarize_statement"
    description = (
        "Rezumă extrasul de cont activ al conversației: banca, perioada, "
        "numărul de tranzacții, totalul intrărilor și ieșirilor, și soldul "
        "net. NU acceptă argumente - folosește întotdeauna extrasul activ."
    )
    input_schema = SummarizeStatementInput
    read_only = True

    def __init__(self, supabase: AsyncClient) -> None:
        self._supabase = supabase

    async def run(self, validated_input: BaseModel, context: Context) -> ToolResult:
        assert isinstance(validated_input, SummarizeStatementInput)
        del validated_input

        if not context.statement_id:
            return ToolResult.failure(
                name=self.name,
                error="Nu există niciun extras activ în această conversație.",
            )

        from app.modules.statements import service as statements_service

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
        total_in_minor = 0
        total_out_minor = 0
        for row in rows:
            amount = float(row["amount"])
            amount_minor = round(abs(amount) * 100)
            if amount >= 0:
                total_in_minor += amount_minor
            else:
                total_out_minor += amount_minor
        net_minor = total_in_minor - total_out_minor

        bank_name = statement.get("bank_name") or "necunoscută"
        period = _format_period(statement.get("period_start"), statement.get("period_end"))

        summary = (
            f"Extras {wrap_statement_content(bank_name)}, {period}, "
            f"{len(rows)} tranzacții, intrări {_format_eur(total_in_minor)}, "
            f"ieșiri {_format_eur(total_out_minor)}, net {_format_eur(net_minor)}"
        )

        return ToolResult(
            name=self.name,
            data={
                "bank_name": bank_name,
                "period_start": statement.get("period_start"),
                "period_end": statement.get("period_end"),
                "currency": statement.get("currency") or "RON",
                "row_count": len(rows),
                "total_in_minor": total_in_minor,
                "total_out_minor": total_out_minor,
                "net_minor": net_minor,
                "summary": summary,
            },
        )


def _format_period(period_start: object, period_end: object) -> str:
    if period_start and period_end:
        return f"{period_start} - {period_end}"
    return "perioadă necunoscută"


def _format_eur(amount_minor: int) -> str:
    value = amount_minor / 100
    return f"-€{abs(value):.2f}" if value < 0 else f"€{value:.2f}"
