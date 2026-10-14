# Schema Validation — AI service vs. the real database

Validated against the production schema export ( 65 tables,
~297 users in `application_users`, ~261k rows in `plaid_transactions`).

This supersedes the guesses in `docs/assumptions.md` for everything covered
below. **Findings are ordered by severity.** Items marked DECISION need a
product/backend answer before the mapping can be finalized.

---

## 1. 🔴 Transaction sign convention was inverted — FIXED

The service assumed `negative = outflow, positive = inflow`. The database
stores the **opposite** — Plaid's actual convention.

Measured over 122,326 real rows:

| Row type | Positive | Negative |
|---|---|---|
| Known purchases (Netflix, Uber, Walmart, …) | 18,300 | 167 |
| Known income (payroll, salary, direct deposit) | 6 | 7,557 |
| All sampled rows | 85.7% | 13.7% |

**Positive = money out. Negative = money in.**

Every income and expense figure the service produced was inverted. Asked
"how much did I spend last month?", it would have summed the user's
paycheques. `get_income()` returned expenses and `get_expenses()` returned
income.

**Why it survived review:** the rule lived as bare `t.amount < 0`
comparisons in eight places across `calculations.py`, `analytics.py` and
`backend_client.py`, and the tests encoded the same inversion. Code and
tests agreed with each other, so everything passed.

**Fix:** the convention now has exactly one definition in
`app/utils/transactions.py` (`is_expense`, `is_income`, `expense_amount`,
`income_amount`). All call sites use it. `tests/unit/test_sign_convention.py`
anchors assertions to real-world meaning — "a salary deposit is income" —
rather than to a sign, so implementation and tests cannot drift together
again. The in-memory test double now generates data in the production convention.

If the backend chooses to normalize signs before serving this service, flip
`OUTFLOW_IS_POSITIVE` and nothing else changes — but that must be an
explicit agreement, recorded in `docs/backend-integration.md`.

---

## 2. 🔴 Two user tables — `user_id` means `application_users.id`

The database has **both** `users` (Laravel's default/admin table) and
`application_users` (real end users: Plaid tokens, timezone, 2FA, profile).

Every foreign key on financial data points at **`application_users.id`**:

```
alert_settings, allo_score_*, chat_sessions, chat_messages, investment_items,
plaid_items, plaid_recurring_streams, subscriptions, notification_settings,
property_*, vehicle_*, user_* ... → application_users(id)
```

Both tables are auto-increment `bigint` starting at 1, so **`users.id = 5`
and `application_users.id = 5` are different people.** If the backend ever
mints a service token whose `sub` carries a `users.id`, this service would
scope every query to the wrong person and return another user's finances —
with no error anywhere.

**REQUIRED of the backend:** the `sub` claim MUST be
`application_users.id`. Nothing in the AI service can detect a violation on
its own, because both values are plausible integers.

Related: `application_users.timezone` is the correct source for
`UserContext.timezone`.

---

## 3. 🔴 Investments need a three-level join — no `user_id` column exists

`investment_holdings`, `investment_accounts`, `investment_transactions` and
`investment_securities` have **no `user_id` column at all**. Ownership is
reachable only through:

```
investment_holdings.account_id → investment_accounts.id
investment_accounts.item_id    → investment_items.id
investment_items.user_id       → application_users.id
```

This is the most dangerous scoping path in the schema. A developer writing
`SELECT * FROM investment_holdings WHERE user_id = ?` gets an error; one
writing `SELECT * FROM investment_holdings` gets **every user's holdings**
and no warning. Any `read_only_db` implementation must join through
`investment_items`, and that query needs a dedicated cross-user test.

---

## 4. 🟠 No foreign keys on the highest-volume financial tables

These have a `user_id` column but **no FK constraint**, so the database
enforces nothing — an orphaned or mis-assigned `user_id` is silently valid:

```
plaid_transactions   plaid_accounts    bills            budgets
categories           liability_accounts liability_credit liability_loans
liability_mortgages  recurring_transactions
investment_snapshots credit_scores
```

`plaid_transactions` alone holds ~261k rows with no referential integrity on
ownership. This is precisely the scenario the AI service's containment
barrier (`app/core/data_isolation.py`) defends against — but the barrier can
only act on records that carry `owner_user_id`, so the backend should stamp
it.

Also: `recurring_transactions.user_id` is `int(122) DEFAULT NULL` — nullable
and a different type from the `bigint(20) UNSIGNED` it should reference.
Nullable ownership on a financial table means rows that belong to nobody and
are excluded from every `WHERE user_id = ?` query.

---

## 5. 🟠 `subscriptions` is not what the service thinks — DECISION

The service models `Subscription` as a recurring **spend** (Netflix,
Spotify) with `name`, `amount`, `billing_cycle`, `next_charge_date`.

The real `subscriptions` table is the **app's own billing plan**:

```
plan_id, app_account_token, transaction_type, is_trial,
trial_start_date, trial_end_date, price, subscription_id
```

Asked "what subscriptions am I paying for?", the service would answer with
the user's MyFi plan instead of their streaming services.

The user's actual recurring spend lives in **`plaid_recurring_streams`**,
which is a genuinely good match:

```
description, amount, frequency, last_payment_date, next_payment_date,
active, plaid_category_id, account_id, user_id
```

**DECISION:** point `get_subscriptions()` at `plaid_recurring_streams`, and
decide whether the app's own billing plan should be exposed to the assistant
at all (recommendation: no).

---

## 6. 🟠 `bills` has no bill in it — DECISION

The service expects `bill_id, name, amount, due_date, is_paid`. The real
table is:

```
id, total_saving_amount, monthly_saving_amount, user_id, timestamps
```

No name, no amount due, no due date, no paid flag. This looks like a savings
summary, not a bills list.

Actual bill-like data appears to live on `plaid_transactions`, which carries
`due_date`, `frequency`, `is_recurring` and `stream_id`, and/or in
`plaid_recurring_predictions`.

**DECISION:** confirm where bills really live before mapping
`get_bills()`. Until then, bill queries should report data unavailable
rather than return savings figures labelled as bills.

---

## 7. 🟡 Field-level mismatches on `plaid_transactions`

| Service field | Real column | Note |
|---|---|---|
| `date` | `transaction_date` | rename |
| `merchant_name` | `name` | no separate merchant column |
| `category` (string) | `plaid_category_id` (FK) | join `plaid_categories.display_name` / `primary_name` |
| `pending` (bool) | `pending` **varchar(255)** | needs parsing, not a cast |
| `currency` | *absent* | `plaid_accounts` has no currency either; `investment_accounts`/`liability_accounts` have `iso_currency_code` |
| — | `is_deleted` | **must filter `is_deleted = 0`** |
| — | `is_review_transfer` | likely must exclude transfers from spending totals |

`plaid_accounts.avaliable_balance` is misspelled in the schema — match it
exactly or the column won't bind.

The missing currency field matters: `require_same_currency()` in
`app/utils/money.py` guards against mixing currencies, but if every
transaction defaults to `"USD"` that guard is inert for users whose accounts
are not USD.

---

## 8. 🟡 `budgets` has no period and no spent amount

Real columns: `category_id`, `amount`, `status`, timestamps. The service's
`BudgetCategory` expects `budgeted_amount` **and** `spent_amount`, and
`get_budgets()` takes `period_start` / `period_end`.

There is no period column, so budgets appear to be perpetual (probably
monthly by convention), and `spent_amount` must be computed from
`plaid_transactions` for the period.

**DECISION:** confirm the intended budget period semantics.

---

## 9. ✅ Conversation storage should move to the database

`chat_sessions` and `chat_messages` already exist, both with
`user_id → application_users(id)`, and `chat_messages.role` is an
`enum('user','assistant','system')` matching `MessageRole` exactly.

This is the drop-in replacement for `InMemoryConversationStore`, which
cannot survive more than one process. Implement `ConversationStore` against
these tables and wire it into `get_conversation_store()` — no orchestrator
change needed.

Note `chat_sessions.id` is a `bigint`, while `UserContext.session_id` is a
string; and `chat_messages` has no `intent` column (`meta` is a JSON column
that could hold it).

---

## 10. ✅ Financial score has a real source

`allo_score_snapshots` / `allo_score_profiles` / `allo_score_status` (and
`credit_scores` / `crs_score_ups` separately) are the authoritative score
tables. `FinancialScore` should read from these and never compute a score
locally, consistent with the existing "explain, never invent" rule.

---

## Summary of what the backend team must guarantee

1. `sub` in the service JWT is **`application_users.id`** — never `users.id`.
2. Independently verify the `user_id` query parameter against the service
   token's own identity (see `docs/backend-integration.md`).
3. Stamp `owner_user_id` on every record returned, then enable
   `STRICT_DATA_OWNERSHIP=true`.
4. Scope investments by joining through `investment_items`.
5. Filter `plaid_transactions.is_deleted = 0`, and decide on
   `is_review_transfer`.
6. Confirm the sign convention is passed through unchanged (positive = outflow).
