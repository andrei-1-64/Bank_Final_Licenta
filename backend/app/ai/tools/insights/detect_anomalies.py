"""Flag transactions that are statistically unusual against the user's own
recent history - explainable (z-score, first-time merchant), not a black box.

Two independent signals, either or both may fire on the same transaction:
- **amount**: further from the baseline mean than `threshold` standard
  deviations.
- **new_merchant**: the only occurrence of this merchant/reference in the
  whole baseline window - not necessarily large, just unfamiliar.
"""

from __future__ import annotations

import statistics
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from app.ai.context import Context
from app.ai.schemas import ToolResult
from app.ai.tools.base import Tool
from app.ai.tools.insights._shared import load_rows

if TYPE_CHECKING:
    from supabase import AsyncClient

DEFAULT_DAYS_BACK = 90
MIN_DAYS_BACK = 1
MAX_DAYS_BACK = 365

DEFAULT_THRESHOLD = 2.0
MIN_THRESHOLD = 1.0
MAX_THRESHOLD = 5.0

#: Below this many baseline transactions, a mean/std-dev is not meaningful.
MIN_BASELINE_SIZE = 5

TOO_FEW_TRANSACTIONS_NOTE = "Prea puține tranzacții pentru analiză statistică"


def _normalized_key(description: str, reference: str) -> str:
    return (description or reference).strip().lower()


class DetectAnomaliesInput(BaseModel):
    """Arguments the model may supply. Validated, but still untrusted."""

    days_back: int = Field(
        default=DEFAULT_DAYS_BACK,
        ge=MIN_DAYS_BACK,
        le=MAX_DAYS_BACK,
        description=(
            "How many days of history establish the user's 'normal' spending "
            "baseline. Defaults to 90."
        ),
    )
    threshold: float = Field(
        default=DEFAULT_THRESHOLD,
        ge=MIN_THRESHOLD,
        le=MAX_THRESHOLD,
        description=(
            "Number of standard deviations from the mean a transaction must "
            "exceed to be flagged by amount. Defaults to 2.0; raise it to see "
            "fewer, more extreme outliers."
        ),
    )


class DetectAnomaliesTool(Tool):
    name = "detect_anomalies"
    description = (
        "Flag the user's unusually large or unfamiliar-merchant transactions "
        "against their own recent spending baseline, across all of their "
        "accounts. Use this for 'anything suspicious', 'unusual spending', or "
        "'weird transactions' questions. Explainable by z-score, not a "
        "black-box model."
    )
    input_schema = DetectAnomaliesInput
    read_only = True

    def __init__(self, supabase: AsyncClient) -> None:
        """`supabase` is the shared process-wide client (see db/supabase_client)."""
        self._supabase = supabase

    async def run(self, validated_input: BaseModel, context: Context) -> ToolResult:
        assert isinstance(validated_input, DetectAnomaliesInput)

        now = datetime.now(UTC)
        date_from = now - timedelta(days=validated_input.days_back)

        entries = await load_rows(self._supabase, context, date_from=date_from, date_to=now)
        spending = [e for e in entries if e.direction.value == "debit"]

        baseline_period = {
            "start_date": date_from.date().isoformat(),
            "end_date": now.date().isoformat(),
        }

        if len(spending) < MIN_BASELINE_SIZE:
            return ToolResult(
                name=self.name,
                data={
                    "baseline_period": baseline_period,
                    "baseline_stats": {
                        "mean_minor": 0,
                        "std_dev_minor": 0,
                        "transaction_count": len(spending),
                    },
                    "anomalies": [],
                    "anomaly_count": 0,
                    "note": TOO_FEW_TRANSACTIONS_NOTE,
                },
            )

        amounts = [e.amount_minor for e in spending]
        mean = statistics.mean(amounts)
        std_dev = statistics.pstdev(amounts)

        merchant_counts: dict[str, int] = {}
        for entry in spending:
            key = _normalized_key(entry.description, entry.reference)
            merchant_counts[key] = merchant_counts.get(key, 0) + 1

        anomalies = []
        for entry in spending:
            z_score = (entry.amount_minor - mean) / std_dev if std_dev else 0.0
            is_amount_anomaly = z_score > validated_input.threshold
            is_new_merchant = (
                merchant_counts[_normalized_key(entry.description, entry.reference)] == 1
            )
            if not is_amount_anomaly and not is_new_merchant:
                continue

            if is_amount_anomaly and is_new_merchant:
                kind = "both"
            elif is_amount_anomaly:
                kind = "amount"
            else:
                kind = "new_merchant"

            anomalies.append(
                {
                    "type": kind,
                    "transaction_id": str(entry.id),
                    "date": entry.created_at.date().isoformat(),
                    "amount_minor": entry.amount_minor,
                    "reference": entry.reference,
                    "z_score": round(z_score, 2) if is_amount_anomaly else None,
                }
            )

        # Amount-based anomalies first (most unusual first), then bare
        # new-merchant flags in chronological order.
        def _sort_key(anomaly: dict[str, Any]) -> tuple[int, float | str]:
            z_score = anomaly["z_score"]
            if z_score is None:
                return (1, str(anomaly["date"]))
            return (0, -float(z_score))

        anomalies.sort(key=_sort_key)

        return ToolResult(
            name=self.name,
            data={
                "baseline_period": baseline_period,
                "baseline_stats": {
                    "mean_minor": round(mean),
                    "std_dev_minor": round(std_dev),
                    "transaction_count": len(spending),
                },
                "anomalies": anomalies,
                "anomaly_count": len(anomalies),
            },
        )
