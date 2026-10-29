"""Safe response-capacity failures, distinct from invalid analysis requests."""


class AnalysisResponseBudgetExceeded(RuntimeError):
    """Even the smallest useful response cannot fit the requested JSON budget."""

    def __init__(self):
        super().__init__(
            "ANALYSIS_RESPONSE_BUDGET_EXCEEDED: narrow the query or increase max_chars"
        )
