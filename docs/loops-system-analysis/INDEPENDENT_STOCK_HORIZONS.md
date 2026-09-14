# Independent stock horizons

Research-selected additions use [Research symbol onboarding](RESEARCH_SYMBOL_ONBOARDING.md): an explicitly selected batch, a 2018 historical floor, included-plan cost checks, candidate training, verified publication, and atomic activation. Read current membership from `datafetching/watchlist.txt` and validate historical publications against their own saved universes.

The accepted stock design covers the production symbols in `datafetching/watchlist.txt`.
All clocks below are America/Los_Angeles. Options remain separate research.

The September 14 production universe contains eleven symbols after verified
CROX/PATH/TWST/IONQ onboarding. All 209 directional execution windows qualified;
learned enrichment remains qualified only for 1h/4h. The manual Gameplan policy
uses the separately verified directional forecasts.

The operator's September 14 exception permits a manual
`--late-opening-date 2026-09-14` only during 04:00–05:00 that date, with the managed
Gameplan policy. It keeps forecast IDs, target ends, live capital/quote checks,
and the durable one-batch-per-hour entry claim. It expires at 05:00; no future
session or recurring launcher inherits it. All normal times below still apply.

| Horizon | Forecast entry times | Holding target | Relative allocation cap |
| --- | --- | --- | --- |
| 1h | 04:00 through 16:00, every hour | Next hour | 1 |
| 4h | 04:00, 08:00, 12:00, 16:00 | Four action hours; 16:00 carries through next exchange session 07:00 | 2 |
| 1d | 04:00 | Same session 17:00 | 3 |
| 1w | 04:00 when no weekly allocation is active | Fifth exchange session 17:00 | 4 |

These are 19 entry opportunities per symbol (209 across eleven stocks), not a
requirement to place an order in every window. Bearish and neutral entry forecasts create no
BUY; scheduled exits can only reduce shares owned by their horizon. The runtime
does not open shorts. A horizon with a working
entry, unclosed position, or uncertain order cannot open another allocation.

The scheduled-default `fixed-horizon-budget-v1` strategy divides the existing per-symbol
allocation ceiling with 1:2:3:4 weights, applies the existing single-order cap,
then uses `min(0.5, max(0, 2*p - 1))` of that horizon budget. Here `p` is the
qualified stock forecast's probability; a long entry requires at least 0.54.
This is an explicit conservative capital rule, with separate policy audit
fields and no invented learned return or profitability estimates. Whole shares, available cash,
gross/symbol exposure, the existing single-order ceiling and six-order batch
ceiling still apply, so final filled sizes are not guaranteed to have those
exact ratios. Exits precede entries in one combined batch. Unfilled exits do
not create spendable cash. Shares already owned manually are not assigned to
any horizon.

The manual Gameplan start selects `gameplan-direction-current-market-v1` instead.
It uses the same promoted 54%/46% forecast directions, buys from the full horizon
capacity permitted by actual cash/exposure, sells eligible current shares on
bearish forecasts, and holds on neutral forecasts. Eligible unallocated manual
shares can be assigned explicitly when a directional sell is reserved; shares
reserved for pending sells or protected by other horizons are excluded. A
reservation is not a fill. The live ledger changes filled inventory only from
broker evidence, and an unfilled sale cannot finance another order.

The user-selected stock direction bands are bullish at P(up) >= 0.54,
bearish at P(up) <= 0.46, and neutral in between. This classification is
independent of model approval. New forecasts record these thresholds; older
published forecasts retain their original labels and measured probabilities.

New independent directional models use `independent-stock-directional-promotion-v2`.
The daily candidate set includes regularized logistic models with C values
0.001, 0.01, 0.1 and 1, selected on development data before assessment. The user
authorized replacing strict baseline wins with operating tolerances: Brier score
may be at most baseline + 0.005 and log loss at most baseline + 0.01. Actual
scores and baseline differences remain recorded; approval under this rule does
not assert outperformance. Probability variation, calibration and assessment
sample requirements remain. Earlier publications retain their recorded strict
policy. Publication, the stock reader and champion retention use the same
versioned numerical assessment implementation.

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
later daily outlooks have no independent entry authority. All `24 × N` option-intent
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

Prospective feature selection uses `independent-gameplan-prior-session-features-v1`:
choose the latest eligible completed hourly feature bar from the immediately
previous exchange session, with recorded availability by that session's 17:05
Pacific cutoff. The bar must finish at or after the actual regular close and
by 17:00. No rolling-model target-clock prerequisite is imposed. The declared
source clocks and selection version travel with forecasts, cohorts and models;
old publications retain their legacy selector. See the full cutoff and source
verification rules in `NIGHTLY_GAMEPLAN.md`.

The selected fixed-budget strategy requires a stock forecast that passed model
validation and the existing entry, ownership, quote, capital and order gates.
The four optional learned return and allocation models use exact execution
outcomes, causal inputs and chronological development/assessment. They do not
determine quantities in the current Gameplan or the fixed-budget strategy.
Only the optional `qualified-enrichment` strategy requires their qualified
sizing evidence. Describe their state as training completed, validation passed
or not passed, and used or not used by the selected strategy; report the actual
failed checks instead of the ambiguous user-facing label "research-only".
Persisted model status identifiers remain unchanged for compatibility.
Selecting fixed budgets does not relabel a model or manufacture an `EnrichmentOutput`.
All strategies preserve the exact target expiry and exclude opening-gap and
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
forecast with adjacent capacity/direction quantities, working prices, current
investment, post-hour cash/shares and Plan action. Failed account/ownership evidence
stops that stage and preserves the prior trade-plan pointer.

Projected Trade Quantity is the whole-share capacity for each opportunity:
account equity times min(0.15 * horizon weight / 10, 0.05), limited by current
cash and remaining account/symbol exposure, divided by the upper planning price.
These per-opportunity alternatives are not added as simultaneous orders and can
be positive beside Sell or Hold. Non-entry outlooks show a dash. The adjacent
Direction Based Trade Qty applies promoted >=54% bullish buys, <=46% bearish
sales of eligible held stock, and Neutral zero through one shared cash balance.
Fresh account evidence includes every configured stock balance, cash and pending-order
reservations, active horizon allocations and options/other exposure. Each clock
processes bearish sales, due exits and bullish buys, with later horizon shares
and pending sales protected. Conditional prior proceeds may fund later buys;
no actual fill is claimed. Row balances are after the entire hourly batch and
match the 04:00–17:00 portfolio rollforward. Later-expiring overnight/weekly
holdings remain in the end-of-day forecast, whose cash is before fees/taxes.

The scheduled-default long-only strategy retains its expandable
confidence-weighted entry preview. The manual Gameplan strategy follows the
same frozen directions while recalculating orders from fresh account and quote
evidence. It does not replay the hypothetical direction ledger or spend its
unconfirmed sale proceeds. `direction-ledger.json` saves every ordered
event, hourly/EOD totals and no-fill baseline; `planning-price-path.json` saves
the conditional working prices and supporting historical evidence.

Price bands use the available observations from the last 120 exchange-session
transitions. Two observed pairs suffice to calculate the median and historical 5th–95th
percentiles of prior-session 17:00 close to clock-price ratios. Historical sample
pairs retain the native five-minute endpoint tolerance and verified source
identity. The planning reference is the exact last completed session's close,
subject only to the bounded planning-only completion policy below.
Broker prices retain their actual market timestamp and appear separately;
the band is not shifted by a later quote that already includes overnight moves.
Missing samples remain unavailable. Main working prices use the observed median
plus/minus 20bps, rounded outward to cents. This is an explicit conditional-fill
assumption, not a confidence interval, stop or execution limit. Wider historical
ranges stay in the evidence for stress analysis. A price outside the working
range never blocks an order: planning prices and cash ranges are estimates only.
Actual orders use current ask prices for BUY limits and current bid prices for
SELL limits, with permitted tick rounding and actual cash/share availability.
New fills and changing prices update actual balances independently of the
overnight cash range; the range is not an execution threshold.

The user-approved planning-only policy may complete a trailing gap at the
exact prior exchange session's 17:00 Pacific reference. A complete verified
source partition must cover through that boundary. The last actual minute close
must come from the same session, with observation age greater than five and no
more than fifteen minutes at 17:00, measured from minute completion. Historical
and Live APIs need not both return the same result. An incomplete source
partition, invalid observation or longer gap remains unavailable.

The missing minutes use the last actual close for all four OHLC fields, zero
volume and `is_synthetic=true`. These rows express a no-trade planning assumption;
they do not prove the absence of trades or represent new provider observations.
The original actual observation time and source identity remain recorded beside
the synthetic completion boundary and gap length. `planning-reference-completion.json`
and `synthetic-reference-bars.parquet` preserve that evidence under the trade-plan
manifest. Entry bands and all 04:00–17:00 working prices share this reference.
Observed archives, historical sample pairs, training, evaluation and actuals
receive no synthetic rows; their five-minute gates remain enforced. Live quote
validation and order behavior are unchanged.

New trade plans use `cash-aware-gameplan-trade-planning-v4`; enabling completion
selects `historical-entry-price-band-v2` and
`conditional-hourly-planning-price-path-v2`. The native price-source and forecast
contracts retain their identities. Legacy v1 planning derivations keep their
strict observed-reference behavior and saved publications remain immutable.

This planning stage cannot submit or cancel orders or activate the live worker.
At execution the selected worker policy recalculates quantity and its current
quote LIMIT with fresh cash, position, quote, ownership, control and risk checks.
Research directional models and sub-threshold signals can have standalone
capacity while producing no direction trade. The main document shows directions,
capacity and direction quantities, working prices, remaining cash/shares and
Plan action. The native scheduled-entry preview and chronological transaction
details are expandable; sample counts and file-integrity details remain in
supporting evidence. See [the complete review contract](NIGHTLY_GAMEPLAN.md#account-aware-trade-plan-review).
Day 1 means the completed source session; the next session is Day 2. Existing immutable Gameplans
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

Routine inventory polls wait while the existing broker execution window is
closed, including the 06:25–06:30 and 13:00–13:05 transitions. They continue
updating the session heartbeat without making broker calls or counting an
expected closed interval as a failed cycle. A prior unresolved failure remains
degraded until a later executable worker cycle captures current broker state.

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

The manual Gameplan policy can wait up to five seconds after a coherent broker
capture for a recently delivered BBO timestamp to reach the local clock. This
handles small host-clock lag without relabeling quotes or moving the portfolio
timestamp, entry deadline, or closing deadline. The existing execution lead,
60-second evidence limit, activation and identity checks still apply. Larger
clock discrepancies and waits that cannot fit the deadline remain blocked.
An unresolved quote block on an owned, due exit reports
`HORIZON_EXIT_QUOTE_UNAVAILABLE` and degrades session health; an ordinary entry
quote skip remains a valid no-trade decision. Windows time synchronization
should still be maintained through its normal administrator-controlled service.

`Loops Operations Watch` uses the existing 90-minute recurrence (verified September 13), renamed from
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

The scheduled-default start still requires both existing stock controls and
begins no earlier than 03:55 on its exchange date. It terminates at 17:00 and
does not wait overnight. Existing legacy schedules must not run independent targets
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

### One-action manual start and automatic wake

The user can launch [`Start-Gameplan-Trader.cmd`](../../Start-Gameplan-Trader.cmd)
once. That manual action enables both stock controls and starts the Gameplan
policy with `--execute --run-session --wait-for-open`; there is no second click,
confirmation, or prompt at 04:00. Creating or testing this launcher does not
activate it. The selected policy is explicit; the existing Scheduled task and
CLI defaults are unchanged.

Before 04:00 Pacific the worker reports `SLEEPING_UNTIL_OPEN`, retains one
process lock, and updates `session-status.json` every 30 seconds or sooner with
`action_date`, `wakes_at`, PID, and heartbeat. It reads local activation controls
only: no broker requests, entry-slot claims, inventory management, or model
preflight run while sleeping. Turning either stock control off ends the wait.
The computer must be running for the process to make progress; this is worker
standby, not a request to suspend Windows.

At 04:00 the worker reads the current Gameplan and begins normal session
management; the first entry batch is 04:01, with the established 13:06 transition
entry. Existing owned positions retain exit management if entry qualification
is unavailable. Starting during an open session starts immediately without
replaying missed slots. A previous-evening, weekend, or holiday start selects
the next supported exchange session; the existing contract skips half days.
Local wall-clock construction keeps the opening at 04:00 across DST changes.

At 03:55 the unchanged Scheduled launcher can adopt this verified manual worker
instead of launching another. Adoption requires an exact recognized policy and
optional wait command, matching launcher/child policy and wait mode, executable
identities, process relationship, creation times, and current lock ownership.
A manual Gameplan start encountering an existing different strategy reports
the mismatch before enabling controls. After its selected session ends at
17:00, this one-session manual worker exits; it does not create another schedule.

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
