# Independent stock horizons

Reviewed October 11, 2026 on Atlas against HEAD
`90704d21801f60426493bd6cc650a7fac38d4f61` plus inventoried working-tree source.
The horizon, ownership and policy contracts remain relevant. Earlier fixed-policy
deployment, schedule and date-specific activation descriptions are history.
Current preparation follows [Stats-first](CURRENT_SYSTEM.md);
actual local scheduling is separate from the shared implementation.

## Forecast windows and policy boundaries

All clocks are America/Los_Angeles. Each publication freezes its configured
universe and target version; a PC's current watchlist does not redefine an old
edition. The independent grid contains 24 rows and 19 execution opportunities
per researched symbol:

| Horizon | Forecast entry windows | Forecast endpoint | Relative budget weight |
| --- | --- | --- | ---: |
| 1h | 04:00 through 16:00 hourly | Following hour | 1 |
| 4h | 04:00, 08:00, 12:00, 16:00 | Four action hours; the last ends next exchange session at 07:00 | 2 |
| 1d | First session 04:00; four later daily rows are outlooks | That session 17:00 | 3 |
| 1w | First session 04:00 | Fifth exchange session 17:00 | 4 |

One opening-gap research row has no entry authority. Stock-only option rows are
explicit non-executable placeholders. A forecast opportunity is not a required
order; short selling is not introduced by this contract.

Forecast endpoints measure model outcomes. They are **not automatic liquidation
times for the current signal-driven Gameplan policy**. Distinguish the policies:

| Policy | Direction/sizing and holdings contract |
| --- | --- |
| `gameplan-direction-current-market-v1` | Reads saved instructions; bullish purchases accumulate within their owning horizon, bearish instructions sell eligible inventory, neutral holds. Accepted-plan execution nets due quantities against fills and reservations. No scheduled expiry liquidation. |
| `fixed-horizon-budget-v1` | Retained legacy alternative: qualified bullish probability supplies `min(0.5, max(0, 2*p - 1))` of the capped weighted budget, with its own entry and scheduled-exit semantics. |
| `qualified-enrichment` | Retained CLI default with separate learned-model qualification and observed scope requirements. An unqualified sizing model is not promoted by selecting another policy. |

The September 16 signal-driven instruction superseded fixed-duration holdings
for the selected manual Gameplan route. It did not rewrite older publications or
remove retained fixed-policy source. The table's weights do not promise filled
quantities in those ratios: whole shares, cash, exposure, ownership and native
batch/order limits still apply.

## Ownership and execution

Atlas's combined Gameplan uses the two research packages and its own trading records. Atlas may synthesize once and return the same completed 22-symbol plan to both PCs. Atlas alone runs the Gameplan worker; Atlas and Scout each separately run their own local 15-minute worker. See [independent traders](../development/independent-traders.md).

The Gameplan native ledger is `state/gameplan-trader/holdings.sqlite3`; its own order authority is `state/gameplan-trader/authority.sqlite3`. Actual broker holdings and working orders bound available capacity. Preserve recorded own fills and order IDs; a manual or other-trader account movement is not a Gameplan fill, a cross-trader discrepancy or a reason to replenish a completed purchase.

Each trader honors its own manual controls. There is no other-trader process check, ledger union, compatibility attestation, approval or verification prerequisite. The only synthesis is the two nightly Gameplans. Preparation and ordinary recovery are permanently authorized.

### Accepted-plan catch-up

The inspected `ml/stock_trader/catchup.py` nets already-due accepted-plan
BUY/SELL quantities within the same symbol and inventory-owning horizon against
confirmed fills and live reservations. Future instructions wait; an earlier
clock alone does not discard an unfulfilled accepted intention during the
supported action day. Opposite intentions can net to zero. Cancelled residuals
can remain due; unknown quantities remain committed. Confirmed broker rejections
require review instead of generating unlimited new identities.

Fallback sale attribution uses its donor horizon. Current quotes, cash and
eligible inventory still bound any residual. This is not replaying every missed
historical order or changing the original forecast's evaluation window. Legacy
per-hour readers retain their own narrower entry rules.



## September 30 prospective bearish fallback

The dated decision introduced the exact frozen
`hierarchical-bearish-fallback-v1` policy for qualifying October 1 and later
publications. [The full policy](CROSS_HORIZON_BEARISH_FALLBACK.md) remains current
where bound by the selected source: ordinary own-horizon sales first, then only
longer donors, at most one donor per forecast, disjoint weighted slot allowances,
50% daily symbol and donor ceilings, and persistent reservation accounting.
Absence from an older source preserves old behavior. Daily reset is based on
actual remaining holdings, not a lifetime floor. The 15-minute design has not
silently extended this policy.

## Versioned forecasts and model evidence

`independent-stock-targets-v1` preserves exact extended-session endpoints and
the five-minute observation tolerance. No regular-session substitute supplies a
missing endpoint. Newer YG targets use strictly positive raw return; older OG
targets preserve cost-adjusted semantics. Direction threshold v2 is above/below
50%, with exactly 50% neutral and explicitly evidenced abstentions preserved.

Model fitting and validation belong to publication. The manual instruction
reader does not rerun model research qualification as an additional execution
veto; source/deployment/account bindings still apply. Fixed and learned policies
retain their different model gates. Operating promotion tolerances do not prove
baseline outperformance. Four immutable training cohorts preserve source,
target, causal clocks and model evidence for separate enrichment training.

Historical prior-session feature and archive-history contracts remain bound to
each edition. XNAS target prices do not become EQUS continuation prices, and
planning-only carried closes do not enter training or actuals. Read
[archive history](NIGHTLY_GAMEPLAN.md#historical-training-integration-approved-september-23-2026)
and [YG provenance](NIGHTLY_GAMEPLAN.md#september-18-yung-gameplan-deployment).

### Nightly quantities and planning prices

The separate plan is a conditional chronological cash/share projection bound
to the exact frozen forecasts and one coherent account snapshot. Standalone
capacity is distinct from direction-based quantities. Combined planning reruns
the ledger once across original producer inputs, rather than summing local
projections. The current holding policy does not add forecast-expiry sales.

Current publication uses a 504-session planning lookback (the helper's generic
default is 120), at least two valid historical pairs and a median ±20bps working
range. Bounded same-session carried-close evidence is explicitly synthetic and
planning-only. Missing references remain unavailable. Actual BUY/SELL limits
use current quotes under the selected execution policy, including its supported
wide-spread midpoint treatment; estimates never veto a live price merely for
being outside their range. See the complete
[planning contract](NIGHTLY_GAMEPLAN.md#account-aware-trade-plan-review).

### Broker recovery and operational supervision

The retained worker has bounded idempotent broker-read recovery and durable
order identities; it must not repeat an ambiguous mutation. Capture failures
record their actual operation and outcome. A fresh heartbeat, independent
diagnostic or no-signal cycle does not clear a failed worker capture. A later
successful coherent worker capture can clear degraded health when no subsequent
failure remains. The session reports unresolved final errors honestly.

Broker closed transitions remain distinct from failed data requests. Inventory
polls can wait without calls while the session is closed. Retained fixed-policy
timed exits and current signal-driven holdings are different; a generic worker
docstring about closing exits does not establish automatic liquidation for
account-mode Gameplan.

On Atlas, the October 11 saved Windows stock-session registration is disabled.
Old statements that the 03:55 fixed-policy launcher is the active automatic
starter are historical. Current native preparation and Trader Representative
definitions have their own responsibilities. Conditional guarded-start authority
does not prove its required reviewed launcher is installed or supersede an
explicit human hold. See the review and
[stock automation](STOCK_TRADER_AUTOMATION.md).

### One-action manual start and automatic wake

The existing [manual launcher](../../Start-Gameplan-Trader.cmd) selects
`gameplan-direction-current-market-v1` and the supported session/wait route.
Its manual activation flag is an explicit user action; reading this document,
publishing source or preparing a plan does not activate it. The launcher and
private account setup have their own readiness checks.

Before opening, a verified waiting worker holds its session lock, updates local
status and reads controls without broker capture or entry-slot claims. It loads
the relevant source after wake. Ordinary entries begin at HH:01, with 13:06
after the broker transition. The session manager can recheck unfinished current
Gameplan intentions, while durable reservations prevent duplicate commitments.
Weekends, holidays, half-day limitations and DST follow the supported calendar.
The process must remain alive; standby is not a Windows wake request.

A late start in the accepted-plan route follows the catch-up contract above.
The September 14 late-opening and one-symbol quote-recovery examples remain
date-specific history and do not authorize another exception. Manual controls,
explicit holds, process/lock identity and single-owner execution remain intact.

## Historical deployment and recovery references

The September 8 fixed-budget rollout, September 14 onboarding/recovery and
September 18 YG source selection were dated observations with their own native
receipts. They do not establish today's membership, model readiness, controls,
schedules or broker balances. Their original detailed text remains recoverable
as recorded in [RELEVANCE_REVIEW.md](RELEVANCE_REVIEW.md).

Keep original publications, ledgers, task memory and receipts. Current source
tests cover target isolation, fixed/manual policy distinctions, reservation
accounting, quote recovery, accepted-plan catch-up and coordinator gates; this
documentation review inspected those tests without running a trading workflow.
