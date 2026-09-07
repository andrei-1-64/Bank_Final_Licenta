"""Insights (analytical) read tools.

Kept apart from `tools/banking` because the two agents' toolsets diverge from
here on: banking tools answer "what is true right now", insights tools feed
analysis over a window of time.
"""

from app.ai.tools.insights.categorize_transactions import CategorizeTransactionsTool
from app.ai.tools.insights.compare_statement_to_ledger import CompareStatementToLedgerTool
from app.ai.tools.insights.compute_spending_stats import ComputeSpendingStatsTool
from app.ai.tools.insights.detect_anomalies import DetectAnomaliesTool
from app.ai.tools.insights.detect_recurring_payments import DetectRecurringPaymentsTool
from app.ai.tools.insights.get_transactions_in_range import GetTransactionsInRangeTool

__all__ = [
    "CategorizeTransactionsTool",
    "CompareStatementToLedgerTool",
    "ComputeSpendingStatsTool",
    "DetectAnomaliesTool",
    "DetectRecurringPaymentsTool",
    "GetTransactionsInRangeTool",
]
