# Independent stock horizons

The accepted stock design covers AAPL, AMZN, GOOG, MU, NVDA, SNDK and COST.
All clocks below are America/Los_Angeles. Options remain separate research.

| Horizon | Forecast entry times | Holding target | Relative allocation cap |
| --- | --- | --- | --- |
| 1h | 04:00 through 16:00, every hour | Next hour | 1 |
| 4h | 04:00, 08:00, 12:00, 16:00 | Four action hours; 16:00 carries through next exchange session 07:00 | 2 |
| 1d | 04:00 | Same session 17:00 | 3 |
| 1w | 04:00 when no weekly allocation is active | Fifth exchange session 17:00 | 4 |

These are 19 entry opportunities per symbol (133 across seven stocks), not a
requirement to force 133 orders. Bearish and neutral entry forecasts create no
BUY; scheduled exits can only reduce shares owned by their horizon. The runtime
does not open shorts. A horizon with a working
entry, unclosed position, or uncertain order cannot open another allocation.

The selected `fixed-horizon-budget-v1` strategy divides the existing per-symbol
allocation ceiling with 1:2:3:4 weights, applies the existing single-order cap,
then uses `min(0.5, max(0, 2*p - 1))` of that horizon budget. Here `p` is the
qualified stock forecast's probability; a long entry requires at least 0.55.
This is an explicit conservative capital rule, with separate policy audit
fields and no invented learned return or profitability estimates. Whole shares, available cash,
gross/symbol exposure, the existing single-order ceiling and six-order batch
ceiling still apply, so final filled sizes are not guaranteed to have those
exact ratios. Exits precede entries in one combined batch. Unfilled exits do
not create spendable cash. Shares already owned manually are not assigned to
any horizon.

## Versioned forecasts and model evidence

`independent-stock-targets-v1` is opt-in. Historical prices are measured against
the same extended-session endpoints as the prospective windows, using the
existing maximum five-minute observation gap and preserving actual observation
timestamps. No regular-session price can stand in for a missing extended-hour
endpoint. The existing
chronological fit/selection/calibration/assessment partitions, maturity rules,
and promotion checks continue to apply. Missing prices cannot be replaced with
regular-session returns. Older immutable Gameplans retain their original target
definitions and evaluations.

The publication retains 24 rows per symbol: 13 forward hourly entries plus one
explicit opening-gap research row, four forward four-hour entries, one daily
entry plus four later daily outlooks, and one weekly entry. The opening-gap and
later daily outlooks have no independent entry authority. All 168 option-intent
rows in a stock-only publication are non-executable placeholders.

The active source is explicitly selected with
`--stock-price-source xnas-itch-archive-v1`. `stock_target_history` refreshes
the native XNAS.ITCH minute archive before evaluation. It requires a zero-dollar
account-specific cost quote, native request identity, and verified partition
receipts. The loader verifies normalized and raw payload hashes, deduplicates
identical overlaps, and rejects conflicting observations. It never merges
XNAS.ITCH labels with EQUS.MINI continuation prices or fills missing prices.
Older canonical-source publications continue to evaluate against their original
source. Live feature collection is a separate source contract.

The native stock-history stage permanently falls back from unavailable recent
Historical coverage to bounded Live replay of the same XNAS.ITCH minute schema.
Replay covers only the completed 04:00–17:00 Pacific action session, with its own
native delivery evidence and immutable receipt. It never relabels EQUS.MINI,
advances a Historical cursor, fills missing minutes or changes an old
publication. Native Live access must already be accepted for XNAS; no plan or
license is purchased. Verified session partitions survive restarts and feed the
normal source loader, evaluation and training. See the permanent fallback
procedure in `NIGHTLY_GAMEPLAN.md` for costs, retention and failure behavior.

The Gameplan publishes `training-cohort-1h.parquet`, `training-cohort-4h.parquet`,
`training-cohort-1d.parquet`, and `training-cohort-1w.parquet` in its immutable
output manifest. Each contains actual returns, causal decision timestamps,
declared and observed endpoints, and the price-source identity. The supervised
`stock_enrichment_training` tail consumes these verified cohorts; it does not
rebuild them from changing market files. A resumed tail retains its pinned
Gameplan source.

The selected fixed-budget strategy requires a genuinely promoted stock forecast
and the existing entry, ownership, quote, capital and order gates. Learned
enrichment remains a separate research/shadow lane. Its four models use exact
execution outcomes, causal inputs and chronological development/assessment;
research models retain their status. The optional `qualified-enrichment`
strategy still requires its own qualified sizing evidence. Selecting fixed
budgets does not relabel an enrichment model or manufacture an `EnrichmentOutput`.
Both strategies preserve the exact target expiry and exclude opening-gap and
later daily-outlook rows from entry authority.

## Ownership and execution

### Nightly quantities and planning prices

Every new full independent-stock overnight run now ends with the required
`gameplan_trade_planning` stage after enrichment. It captures account balances,
all working-order cash reservations, current holdings/exposure and timestamped
Schwab quote references through read-only methods. It writes a separate immutable
`ml/gameplan-trade-plan-runs/<generation>/Gameplan.md` and `trade-plan.parquet`,
bound to the exact source Gameplan receipt, plus the
`ml/gameplan-trade-plan-latest/run.json` pointer. The review contains every frozen
forecast with Trade Quantity, Trade Price (planning range), current investment,
cash evidence and explicit no-entry reasons. Failed account/ownership evidence
stops that stage and preserves the prior trade-plan pointer.

The quantity proposal reuses the selected fixed-confidence budget and all its
capital ceilings, additionally bounds planning by literal cash rather than
margin capacity, and reserves shared cash at the upper range price. It never
funds entries from expected exits or assigns manual shares to a horizon.
Later entries after a proposed horizon allocation require confirmation of its
exit; overnight proposals are not promises of future fills or spendable cash.

Price bands use the last 120 exchange-session transitions with at least 30
valid observations per stock/entry clock. They are the historical 5th–95th
percentiles of prior-session 17:00 close to entry-price ratios, anchored to the
exact last completed session close in the Gameplan's verified price dataset.
The native five-minute endpoint tolerance and source identity remain enforced.
Broker prices retain their actual market timestamp and appear separately;
the band is not shifted by a later quote that already includes overnight moves.
Missing samples remain unavailable. Bands are descriptive historical ranges,
not qualified price forecasts, stop prices or executable limit orders.

This planning stage cannot submit or cancel orders and does not change the live
worker. At execution the existing worker recalculates quantity and its rounded
ask LIMIT with fresh cash, position, quote, ownership, control and risk checks.
Research directional models and sub-threshold signals retain zero proposed
quantity even when a historical range is available. Existing immutable Gameplans
and previously recorded narrower resume boundaries remain unchanged.

`horizon_ledger.py` records reservations and complete cumulative fills by stable
account fingerprint, symbol, horizon and forecast. An accepted submission never
creates inventory. Unknown submission outcomes retain reservations and block
another attempt. Exact-once entry batches survive process restarts. A manual
share reduction or unexplained account discrepancy blocks further allocation.

Order evidence is captured before a fresh portfolio snapshot. The broker's
credential-generation identity is checked separately from the stable account
identity. Both stock controls, current sessions, forecast entry grace, source
checksums, quote/account age and sizing limits are checked at execution.

The scheduled worker starts at 03:55 Pacific and attempts entry batches at HH:01.
It checks owned exits before new entries. At 13:00, it waits for the
broker's 13:05 POST opening, checks exits, then attempts the entry at 13:06.
Extra wakes reconcile or close existing positions and cannot create extra
entry batches. Expired, tracked BUY orders receive at most one cancellation
request; shares stay reserved until the broker confirms their disposition.

An exit at the 17:00 close must be submitted before the market closes. The
worker starts those exits at 16:59 and checks the remaining allocations every
five seconds. Limit orders do not guarantee fills. Any residual remains owned
by its original horizon and is reported for the next executable session;
it must never be declared flat merely because the target expired.

## Commands and deployment state

Training/publication uses the native supervised overnight owner and its original
04:00 publication deadline:

```powershell
.\.venv\Scripts\python.exe -u -m ml.overnight_runtime --datastore-target pc --once --stock-only --independent-stock-horizons --stock-price-source xnas-itch-archive-v1
```

A non-submitting inspection of a published independent plan:

```powershell
.\.venv\Scripts\python.exe -m ml.gameplan_stock_trader --datastore-target pc --target-horizon all --sizing-policy fixed-horizon-budget-v1
```

The selected bounded session command is:

```powershell
.\.venv\Scripts\python.exe -u -m ml.gameplan_stock_trader --datastore-target pc --target-horizon all --sizing-policy fixed-horizon-budget-v1 --run-session --execute
```

### Broker recovery and operational supervision

The independent worker uses the existing bounded broker-read recovery policy:
three-second retry spacing, at most 120 seconds for starting retries, and a
shorter budget near the current entry deadline or 17:00 close. Every attempt
recaptures order/fill evidence followed by a complete account, open-order and
quote snapshot. Authentication/identity failures and ambiguous broker writes
are not automatically repeated. Existing entry deadlines, controls, durable
slot claims and order identities still apply after recovery.

Decision receipts and session output include `broker_state_capture` attempt,
duration and failing-operation metadata. The current owner writes
`state/independent-stock-trader/session-status.json` at least every normal
30-second poll outside a bounded broker capture. `DEGRADED` persists after a
failed cycle until a later worker cycle succeeds. An unresolved last failure
at 17:00 produces `SESSION_FINISHED_WITH_ERRORS` and a nonzero CLI exit.
An independent successful diagnostic does not itself clear worker health.
A no-signal cycle that does not capture broker state also cannot clear an
unresolved failure. Recovery requires a successful worker capture marked
`CURRENT` or `CURRENT_AFTER_RETRY`, with no subsequent cycle failure or stopped
submission.

`Loops Operations Watch` is the existing ten-minute schedule, renamed from
`Loops Overnight Health Watch`. It now covers daytime failure diagnosis and
tested repairs as well as the documented overnight workflow. It verifies
process/lock identities, heartbeat, actual cycle results and broker evidence;
a living PID alone is insufficient. Repairs preserve the authorized strategy
and all execution safeguards. A necessary controlled restart must first verify
that no order mutation is in flight, preserve entry claims and ledgers, and
verify the replacement owner. Its notifications are enabled. The 03:55 stock
schedule remains the normal starter and the worker still ends at 17:00.

Windows Task Scheduler owns the deterministic 03:55 weekday process start as
`Ducketz Independent Stock Session`, using `docs/datafetch-ml/start_stock_session.ps1`.
The launcher waits for the native session and propagates its exit code. It can
supervise an already verified session without launching a duplicate. The task
uses the current interactive Windows account, starts when available, can wake
the computer, and retries a failed process up to five times at one-minute
intervals. The native calendar and 17:00 cutoff apply to every start. The
existing Codex 03:55 task supervises this OS-owned session; its own completion
does not determine whether the native process starts.
Adoption validates the full deployed command and executable paths, the unique
launcher/child relationship, and their creation times against the native lock.
Conflicting command arguments or a lock from an older process instance are
rejected before waiting on the existing worker.

Deployment also requires both existing stock controls. Start no
earlier than 03:55 on its exchange date. It terminates at 17:00 and does not
wait overnight. Existing legacy schedules must not run independent targets
through the shared-position executor; that adapter explicitly rejects them.

The existing stock automation starts this worker at 03:55 Pacific on
weekdays. The former separate 13:05 launch is paused. Nightly preparation and
its health watch use the stock-only independent-target and explicit XNAS source
flags, including the history and enrichment stages. The nightly and health-watch
schedules, models, reasoning settings, notification policies and existing
workspace paths were preserved.
Live entries require the session manager; a one-shot call can still manage
owned-position exits. The deployment explicitly selects fixed budgets; the CLI
default remains `qualified-enrichment` and retains that strategy's qualification
gate.

September 8, 2026 selected deployment: COST activation is complete. Current XNAS
Gameplan `20260908T093314.374067Z` contains 168 forecasts and 168 stock-only
placeholders; all seven stocks and all 133 execution windows have promoted
forecast authority. The daily forecast retains the verified same-day champion
from `20260908T085844.073361Z`; its unsuccessful challenger remains research.
The native $0 history backfill completed 35 chunks and approximately 1.79 million
minute rows, without mixing price sources or changing promotion thresholds.
The verified UI adapter shows seven published symbols, no pending symbols,
168 rows and seven weekly snapshots; COST's obsolete supplemental badge is cleared.

Learned enrichment `20260908T092028.062383Z` remains research with zero qualified
scopes. It does not block the explicitly selected fixed-budget strategy. All
133 current execution forecasts are bearish or neutral, so there are zero
bullish entry opportunities and the ready worker should submit no BUY. No order
is forced to demonstrate readiness. The verified broker/ledger snapshot has
zero working orders and zero owned horizon allocations; the manually held one
share of each original six stocks remains outside horizon ownership and untouched.
Native receipts and compact diagnostics are in
`artifacts/analysis/cost-onboarding-implementation/`.
