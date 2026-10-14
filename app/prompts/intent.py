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
SAVINGS_ANALYSIS - "how much can I save", savings rate, and savings-rate TREND over time ("is my savings rate improving", "am I saving more than before") -- this intent already carries 12 months of history, so a trend question with no explicit two named periods belongs here, not PERIOD_COMPARISON
SAVINGS_GOAL_QUERY - progress toward a stored savings goal, required monthly pace, or projected completion date ("am I on track for my goal", "when will I reach my savings goal")
CASH_FLOW_ANALYSIS - inflow vs outflow over a period
AFFORDABILITY_ANALYSIS - "can I afford to spend $X"
FORECAST_QUERY - projecting future SPENDING or income as a single estimate (not cash, not week-by-week -- those are LIQUIDITY_QUERY)
INVESTMENT_QUERY - investment holdings/performance
LIABILITY_QUERY - debts/loans/credit balances
FINANCIAL_SCORE_QUERY - a financial health score the backend already computes
ALLO_SCORE_QUERY - the app's own 0-100 Allo Score, with flow/stability/retirement breakdown -- distinct from a credit score
NET_WORTH_QUERY - total assets (accounts + investments) minus total liabilities, "what am I worth"
RETIREMENT_QUERY - retirement readiness: target retirement age, projected future assets, readiness ratio, required savings ("am I on track for retirement", "how much should I save for retirement", "how many years behind am I")
LIQUIDITY_QUERY - spendable-cash questions: "how much can I safely spend", "safe to move to savings", "financial cushion", "emergency fund" (months covered / is it healthy), "how many days will my cash last", "cash left after my bills", "will I have enough through the end of the month", "guilt-free spending", "if I lost my income how long could I cover expenses", "projected cash flow / cash by the end of the month", "which upcoming week will be the hardest financially", "should I use my savings to pay down debt", "should I invest more this month"
DEBT_SCENARIO_QUERY - debt what-ifs and status: "how much faster if I pay an extra $X", "what if I paid off my smallest debt", "are any payments late or at risk"
SAVINGS_OPPORTUNITIES - "where can I save money", "what could I cut back on", "easiest ways to spend less"
SPENDING_SINCE_PAYCHECK - spending since the most recent paycheck / payday
FINANCIAL_OVERVIEW - broad recap across everything: "catch me up", "summarize my finances", "how am I doing overall this week", "why isn't my net worth growing"
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
