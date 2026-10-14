from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.core.security import UserContext
from app.schemas.financial import DataAvailability
from app.schemas.planner import ExecutionPlan
from app.services.financial_service import FinancialService
from app.utils.dates import current_date_in
from app.utils.money import require_same_currency


async def get_liabilities_tool(
    financial_service: FinancialService, context: UserContext, plan: ExecutionPlan
) -> dict:
    liabilities = await financial_service.get_liabilities(context)

    # Same distinction as investments: no debts on file is not proof of no
    # debt. Reporting "total_balance: 0" for a user whose credit accounts
    # simply are not linked congratulates them on being debt-free.
    if not liabilities:
        return {
            "availability": DataAvailability.MISSING.value,
            "liabilities": [],
            "liability_count": 0,
            "total_balance": None,
            "note": (
                "No debts or credit accounts are on file for this user. This means NOT CONNECTED, "
                "not debt-free. Say the data is not available -- do not congratulate them on "
                "having no debt."
            ),
            "currency": context.currency,
        }

    require_same_currency([li.balance.currency for li in liabilities])
    total_balance = sum((li.balance.amount for li in liabilities), Decimal("0"))
    result: dict = {
        "availability": DataAvailability.AVAILABLE.value,
        "liabilities": [
            {
                "name": li.name,
                "type": li.type,
                "balance": str(li.balance.amount),
                "interest_rate": str(li.interest_rate) if li.interest_rate is not None else None,
                "minimum_payment": str(li.minimum_payment.amount) if li.minimum_payment else None,
            }
            for li in liabilities
        ],
        "liability_count": len(liabilities),
        "total_balance": str(total_balance),
        "currency": context.currency,
    }

    # Trend is OPTIONAL: "is my debt getting better or worse?" needs a prior
    # total, "how much debt do I have?" does not. Omitted, not fabricated,
    # when there is no history.
    history = await financial_service.get_liability_history(context)
    if history:
        latest = history[0]
        # The newest snapshot usually IS today's balance (the backend snapshots
        # on the 1st and the live figure has not moved), so comparing the two
        # reported "no change" for a user who had paid down $1,650. When they
        # agree, the meaningful comparison is the snapshot BEFORE it.
        baseline = latest
        if abs(latest.balance.amount - total_balance) < Decimal("0.01"):
            baseline = next((h for h in history if h.snapshot_date < latest.snapshot_date), latest)

        result["trend"] = {
            "previous_total": str(baseline.balance.amount),
            "previous_snapshot_date": baseline.snapshot_date.isoformat(),
            "change": str(total_balance - baseline.balance.amount),
            "note": "A negative change means debt went DOWN (progress).",
        }

        today = current_date_in(context.timezone)
        year_start = date(today.year, 1, 1)
        year_start_snapshot = next((h for h in history if h.snapshot_date <= year_start), None)
        if year_start_snapshot is not None:
            result["trend"]["year_to_date_paydown"] = str(year_start_snapshot.balance.amount - total_balance)
            result["trend"]["year_start_snapshot_date"] = year_start_snapshot.snapshot_date.isoformat()
        else:
            # History does not reach January. Give the longest span there is,
            # labelled as such, instead of "no paydown this year".
            earliest = history[-1]
            result["trend"]["earliest_snapshot"] = {
                "date": earliest.snapshot_date.isoformat(),
                "total": str(earliest.balance.amount),
                "change_since": str(total_balance - earliest.balance.amount),
            }
            result["trend"]["year_to_date_note"] = (
                f"Debt history only goes back to {earliest.snapshot_date.isoformat()}, so a "
                "year-to-date figure cannot be computed. Report the change since the earliest "
                "snapshot and say the history starts there."
            )

    # Debt-to-income, from what was actually paid and earned in the asked
    # period. Required minimum payments are on file for only some debts, so a
    # ratio built from them alone would be far too low; observed loan payments
    # are the honest numerator, labelled as observed.
    if plan.date_range is not None and (plan.date_range.end - plan.date_range.start).days <= 62:
        try:
            income_txns = await financial_service.get_income(
                context, start=plan.date_range.start, end=plan.date_range.end
            )
            spending = await financial_service.get_expenses(
                context, start=plan.date_range.start, end=plan.date_range.end
            )
            period_income = sum((abs(t.amount) for t in income_txns), Decimal("0"))
            loan_paid = sum(
                (abs(t.amount) for t in spending if (t.category or "").lower() == "loan payments"), Decimal("0")
            )
            minimums = [li.minimum_payment.amount for li in liabilities if li.minimum_payment]
            result["debt_to_income"] = {
                "period": {"start": plan.date_range.start.isoformat(), "end": plan.date_range.end.isoformat()},
                "income_in_period": str(period_income),
                "loan_payments_made_in_period": str(loan_paid),
                "percent_of_income_to_loan_payments": (
                    str((loan_paid / period_income * Decimal("100")).quantize(Decimal("0.1")))
                    if period_income > 0 else None
                ),
                "required_minimums_on_file": str(sum(minimums, Decimal("0"))) if minimums else None,
                "minimums_on_file_for": f"{len(minimums)} of {len(liabilities)} debts",
                "note": (
                    "Observed loan payments divided by income for the period. Required minimum payments "
                    "exist for only some debts, so they are shown separately and NOT used as the numerator."
                ),
            }
        except Exception:  # noqa: BLE001 - supplementary; the debt list stands without it
            pass

    return result
