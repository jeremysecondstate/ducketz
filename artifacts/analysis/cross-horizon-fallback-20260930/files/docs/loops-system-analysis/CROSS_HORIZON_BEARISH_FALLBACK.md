# Bounded cross-horizon bearish sales

The operator approved this future-session change on September 30, 2026, with a
50% shared daily ceiling and execution beginning with a newly prepared October
1 or later Gameplan. Today's running worker and September 30 publications keep
their existing behavior. The schedule remains 21:05 Pacific, with the usual
next-session 04:00 preparation deadline and 04:01 first entry.

## Ownership and precedence

| Signal | Normal owned stock | Fallback donors, in order |
|---|---|---|
| 1h | 1h | 4h, 1d, 1w |
| 4h | 4h | 1d, 1w |
| 1d | 1d | 1w |
| 1w | 1w | None |

Existing eligible unallocated stock remains part of the normal route. Fallback
is available only when the normal route genuinely has no stock. A pending buy,
reserved sale, unknown submission, failed reconciliation, or unavailable quote
cannot manufacture that condition. Normal bearish sales are considered before
fallback at the same clock. A donor with a simultaneous bullish instruction,
normal own-horizon sale, or prior-batch pending order is excluded. Disjoint
fallback reservations within the same batch may share one donor under atomic
budget checks.

One triggering forecast selects at most one donor allocation. The closest
longer eligible horizon has priority; unused allowance is not cascaded into a
second order. The donor retains ownership until actual confirmed SELL fills
reduce its holding. The record separately identifies the triggering forecast
and horizon. No ownership transfer, synthetic BUY, short sale, substitute
forecast ID, or second order identity is created.

## Daily limits and weighted pacing

The first ready, reconciled live account snapshot on an action date freezes the
initial owned shares of all 4h, 1d and 1w allocations. The symbol's shared daily
fallback budget is half that total, rounded down. Each donor allocation has its
own half-of-initial-shares ceiling, also rounded down. Unallocated and hourly
stock, projected purchases, and later purchases do not enlarge these budgets.

The 18 scheduled opportunities are ordered by clock, then 1h, 4h and 1d:
13 hourly opportunities, four four-hour opportunities, and one daily opportunity.
They receive weights 1, 2 and 3 respectively, totaling 24 weight units. For daily
budget B, a slot with cumulative preceding weight W and its own weight w gets:

`floor(B * (W + w) / 24) - floor(B * W / 24)`

Thus 300 eligible initial shares create a 150-share daily ceiling: each hourly
slot allows 6 or 7, each four-hour slot 12 or 13, and the daily slot 19. These are
maximum fallback quantities; actual availability, donor caps, pending orders,
current quotes and existing order-risk limits can reduce or prevent a sale.
Bullish, neutral, skipped or unused opportunities do not pass their allowance to
another slot. A retry does not create another allowance.

Confirmed fills plus all still-reserved or unknown quantities consume each
limit atomically. A partial fill of two from a five-share reservation still uses
five until the remaining three is conclusively filled or cancelled. Exact
terminal evidence releases only the unfilled part. Duplicate forecast handling,
process restarts and new source pointers cannot reset the day's baseline.

The operator selected a daily cap, not a lifetime floor. The next session starts
from actual remaining holdings: fallback alone could reduce 300 to 150 on one
day and 150 to 75 on the next if all allowances are usable. Normal own-horizon
bearish sales remain unchanged and can sell that horizon's own available stock;
the fallback ceiling does not cap those ordinary sales.

## Source binding, reporting and review

New independent publications bind `cross_horizon_fallback_policy` consistently
in gameplan.json, manifest configuration and receipt. Only the exact supported
`hierarchical-bearish-fallback-v1` policy activates the behavior. Its absence in
an older immutable publication preserves the old behavior. Execution reads the
exact loaded publication and rechecks the binding before submission. A changed
or inconsistent source fails closed. Legacy fixed sizing stays separate.

Planning uses the same hierarchy, slot quotas and donor ceilings, with its own
explicitly hypothetical snapshot and conditional cash ledger. It records the
donor and triggering horizon. Saved planning uses `allocation_id_sha256` and
`donor_allocation_id_sha256`, stable hashes of the native allocation ID, so
reports contain no raw account or broker identifiers. Live evaluation can join
by hashing its internal allocation ID. Those estimates never become live budget or fill
authority. Actual execution uses a fresh reconciled account snapshot and current
quotes; projected proceeds are never spendable cash.

After each session, review fallback decisions separately from normal sales:
triggering horizon and forecast, donor allocation/horizon, requested quantity,
confirmed fills, open/unknown reservations, cancellations, price and budget use.
Keep forecast accuracy on its original frozen window; attribute the actual
inventory reduction to the donor. Market-price outcomes are not broker fills or
realized profit. A useful comparison needs transaction costs and a clearly
identified no-fallback counterfactual; increased activity alone is not evidence
of improved results. Any change from 50% requires a new explicit operator choice
and a new versioned future policy, never an intraday rewrite or automatic tuning
from one favorable or unfavorable session.

The read-only session observation command is:

```powershell
.\.venv\Scripts\python.exe -m ml.stock_trader.fallback_review --datastore-root C:\DATASTORE --action-date YYYY-MM-DD --output-dir C:\dev\ducketz\artifacts\analysis\fallback-session-reviews\YYYYMMDDTHHMMSSZ
```

Use a fresh output directory for each observation; existing outputs are never
overwritten. It reads a coherent local ledger snapshot without initializing or reconciling
the database, making broker requests, or submitting orders. It saves a fresh
timestamped observation after the normal nightly preparation/actuals tail.
An absent baseline is explicitly unavailable, not evidence of zero sales.
Review unknown/open quantities before drawing conclusions from confirmed fills.

## Deployment while a session is running

Development and tests take place in an isolated checkout. Apply the reviewed
file set only after the current trader is terminal and absent, with a fresh
overnight supervision claim and the native session/cycle locks. Preserve ledger,
controls, all existing reservations, models, publications and concurrent edits.
Verify exact base/final file hashes and rerun the targeted checks before the
next fresh nightly preparation. A live or waiting worker, source drift, or
failed verification blocks deployment; it does not authorize stopping a worker.
Do not start a trader, create a second pipeline, or replay September 30 to test
the change. The user retains the existing manual start action.
