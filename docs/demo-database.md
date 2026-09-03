# Demoing against the real database

The mock backend invents plausible numbers. That is right for tests and wrong
for a client demo: a figure on screen is a claim about the product, and a
number produced by a seeded random generator cannot back that claim.

This mode runs the assistant against a **read-only local copy of the real
MyFi database**, so what appears on screen is what is actually in the data.

## Build it once

```powershell
python scripts/build_demo_db.py "shaktiparastechn_myfi 5.sql"
```

The dump is ~169 MB and is read as a stream. Only the tables the service
actually reads are copied:

| Table | Rows | Used for |
|---|---:|---|
| `application_users` | 130 | the user picker |
| `plaid_accounts` | 176 | balances, credit liabilities |
| `plaid_transactions` | 122,326 | everything about spending and income |
| `plaid_categories` | 104 | readable category names |
| `plaid_recurring_streams` | 492 | subscriptions, bills, recurring |
| `credit_scores` | 47 | the financial score |

Everything else in the dump — sessions, access tokens, notifications, market
data — is skipped. Passwords and tokens are never copied anywhere. Result:
`data/demo.sqlite3`, about 24 MB.

## Point the service at it

```ini
BACKEND_INTEGRATION_MODE=sqlite
DEMO_DB_PATH=data/demo.sqlite3
```

Restart the API (`.env` is read at startup only). The user picker in
`scripts/demo_ui.py` then fills itself from the database — every user with at
least 50 transactions, newest data first, labelled with their id, name where
there is one, transaction count, and the date their data ends.

**37 users** qualify, ordered best-demo-first. Each label says exactly what
that user has, e.g.:

```
User 267 - Mike  (2,224 txns, September 2024 to August 2026, current +score)
User 264         (3,893 txns, September 2024 to June 2026)
```

`current` means their data reaches the newest month in the database;
`score` means they have a credit score.

### The suggested questions follow the selected user

This is the part that makes the whole list usable. Only **six** users have
transactions in both the current and previous month, and only **one** (267)
also has a credit score. Asking every user "how much did I spend last month?"
therefore returns `$0.00` for most of them — correct, and it looks broken.

So the "Try asking" panel is built per user, from that user's real data
window. Pick user 264, whose data ends in June 2026, and the suggestions
become:

- How much did I spend in **June 2026**?
- What were my top spending categories in **June 2026**?
- Compare my spending in **June 2026** to **May 2026**

Which answers with full data:

> You spent $25,022.57 in June 2026.
> Top categories: General Services $12,345.89, Loan Payments $5,298.05,
> Transfer Out $4,142.00.

The "What is my financial score?" and subscription prompts only appear for
users who have that data, so no suggested question can return nothing.

Nine users have data running into August 2026, which is what an unqualified
"this month" needs:

| User | Transactions | Data through |
|---|---:|---|
| 276 — Meiko Lyles | 3,780 | 2026-08-10 |
| 267 — Mike | 2,224 | 2026-08-10 |
| 283 | 3,554 | 2026-08-09 |
| 293 | 3,369 | 2026-08-09 |
| 292 | 2,811 | 2026-08-09 |
| 289 | 3,463 | 2026-08-07 |
| 290 | 2,839 | 2026-08-07 |
| 287 — tau | 3,429 | 2026-08-01 |
| 284 — Teddy | 3,486 | 2026-07-30 |

Users further down the list have data ending earlier in 2026. They demo just
as well — the suggested questions name their months — but an unqualified
"this month" will correctly report nothing for them.

**User 267 is the only one with every field populated**, including a credit
score — but it also carries a pair of junk test rows (−$500,000 and
+$500,000, the latter categorised "TAX REFUNDS"), which pushes its August
spending total to $507,693.74. The assistant reports that honestly, naming
the $500,000 refund as the reason, but it is not a total you want on camera.

**Every capability works for every user.** Affordability, comparisons,
day-by-day breakdowns, categories — none of it is user-specific. Verified
across all 37: the same question produces a verdict for every one of them,
and the verdicts differ because the finances do, not because the feature
does.

| User | "Should I buy a 4K OLED TV?" ($1,550 est.) |
|---|---|
| 264 | comfortable — $5,884.76 left |
| 287 | comfortable — ~$2,049 left |
| 283 | tight — ~$503 left |
| 276, 293 | not affordable — goes negative |

What differs between users is **how much data they have**, which decides
which *questions* land well. Pick by what you are demonstrating:

| If you want to show | Use | Why |
|---|---|---|
| Spending, categories, comparisons, day-by-day | **276** or **283** | Clean data, both months, realistic totals |
| A financial score | **267** | The only demo-ready user who has one — avoid asking about August |
| Affordability with a healthy balance | **264** | $7,434 uncommitted, no upcoming bills |

### A user with no linked accounts

Real users exist who have never connected a bank. Their balance is
**unknown**, not zero — and the two were treated the same, which answered
"can I afford this?" with a confident "no" for every one of them. The
affordability tool now returns no verdict at all in that case, and the
assistant says no connected account was found rather than declaring the
purchase unaffordable. A genuine zero balance on a real account still gets a
normal verdict.

### Rows that make totals look wrong

Six users carry test rows large enough to distort any total that includes
them. These are in the dump, not introduced here:

| User | Largest row |
|---|---:|
| 215 | $9,000,000.00 |
| 217 | $1,000,000.00 |
| 267 | $500,000.00 |
| 210, 225 | $88,400.00 |
| 294 | $60,000.00 |

Every other demo-ready user (276, 283, 287, 292, 293) is clean.

## Safety

- The connection is opened `mode=ro` at the URI level, so this process cannot
  write to the file even by accident.
- Every query filters on `context.user_id`, and every record is stamped with
  `owner_user_id` so `verify_ownership` re-checks it at the boundary. The
  usual cross-user test holds against real data: three users asking the same
  question with the *same session id* get three different answers.
- Production is unaffected. `validate_runtime_safety()` refuses to start in
  production with this mode, alongside `mock`.

## What the real data taught us

Four things that the mock could never have surfaced, all now handled:

**Manual transactions have no `transaction_id`.** 56 rows are entered by hand
in the app rather than imported from Plaid, and that column is null for them.
Skipping them made reported totals disagree with the database by exactly the
amounts a user had typed in — user 267's July was understated by $500. They
are identified by primary key instead.

**`is_review_transfer` flags rows FOR REVIEW — it does not identify transfers**
— 2,252 rows carry it, but only 911 are categorised `TRANSFER_IN` or
`TRANSFER_OUT`. The other 1,341 are ordinary purchases, 438 of them
`FOOD_AND_DRINK`.

Excluding every flagged row (which the demo originally did) understated user
267's food and drink spending by 45% — $1,139.53 reported against $2,084.37
actual — and made named merchants vanish: KFC, Little Caesars and Cheesecake
Summerlin were all in the database and reported as "no transactions found".

So the flag is a **question** and the category is the **answer**: a flagged row
is excluded only when its category agrees it is a transfer. The duplicate risk
this was guarding against is already handled by `is_deleted` — each flagged
purchase has a deleted twin — and across 117,631 live rows only 22
user/amount/date groups could double-count at all.

**This is still the backend team's call**, but the demo now implements the
narrower rule, and the wider one is measurably wrong.

**The `subscriptions` table is the app's own billing**, not the user's
Netflix — it holds `plan_id`, `price`, trial dates. User-facing subscriptions
come from `plaid_recurring_streams`: a monthly recurring outflow.

**There is no budgets table and no holdings table.** Both return empty, so the
assistant says budgets are unavailable, which is true. Deriving a "budget"
from past spending would put an invented target on screen and attribute it to
the user.

## Known rough edges in the data

Worth knowing before a recording, because they are in the data, not bugs:

- **Account names and balances repeat across users.** This is a Plaid
  *sandbox* database: the same test institution ("Adv Plus Banking") was
  linked for many users, so several share an account name and balance. The
  `account_id` values are all distinct — 176 unique across 176 rows — and the
  transactions differ, so totals and categories are genuinely per-user. But
  "what is my checking balance?" looks similar for several users. **Spending
  questions demo far better than balance questions.**
- **Some names are literally `test`, and many are null.** The picker leads
  with the user id for that reason.
- **Six users carry junk test rows** (see the table above). 267 is the one to
  watch, because it is otherwise the most complete record. 276, 283, 287,
  292 and 293 are clean.
- **Categories are Plaid constants**, shown as `General Services`,
  `Food And Drink`. `General Services` dominates most users' spending.

## Switching back

```ini
BACKEND_INTEGRATION_MODE=mock
```

The test suite is unaffected by either setting: it builds its own fixtures.
