INTENT_CATALOG_V1 = """\
GENERAL_CONVERSATION - greetings, thanks, small talk, questions about the assistant itself
TRANSACTION_QUERY - listing/finding specific transactions
SPENDING_ANALYSIS - "how much did I spend", general spending totals
INCOME_QUERY - income received
BALANCE_QUERY - current account balances / "how much money do I have"
BILL_QUERY - upcoming or past bills
BUDGET_QUERY - budget status/adherence
SUBSCRIPTION_QUERY - recurring subscriptions specifically
RECURRING_EXPENSE_QUERY - recurring expenses more broadly (not just subscriptions)
CATEGORY_ANALYSIS - spending broken down by category, "what category am I spending the most on"
MERCHANT_ANALYSIS - spending at a specific merchant
SPENDING_SPIKE - "when did my spending spike", "why was my spending higher"
PERIOD_COMPARISON - comparing two time periods
SAVINGS_ANALYSIS - "how much can I save", savings rate
CASH_FLOW_ANALYSIS - inflow vs outflow over a period
AFFORDABILITY_ANALYSIS - "can I afford to spend $X"
FORECAST_QUERY - projecting future spending/income
INVESTMENT_QUERY - investment holdings/performance
LIABILITY_QUERY - debts/loans/credit balances
FINANCIAL_SCORE_QUERY - a financial health score the backend already computes
FINANCIAL_CALCULATION - an explicit ad-hoc calculation request not covered above
OVERALL_FINANCIAL_HEALTH - "how am I doing financially", broad multi-area assessment
UNKNOWN - cannot be classified into any of the above
"""

INTENT_CLASSIFICATION_PROMPT_V1 = f"""\
Classify the user's message into exactly one of the following intents, \
using the recent conversation for context on follow-up questions (e.g. \
"what about the month before?" inherits the prior message's intent).

{INTENT_CATALOG_V1}

Return only the intent enum value.
"""
