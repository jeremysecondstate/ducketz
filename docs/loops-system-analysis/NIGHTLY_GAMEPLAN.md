# Overnight immutable gameplan

Research-selected additions use [Research symbol onboarding](RESEARCH_SYMBOL_ONBOARDING.md): an explicitly selected batch, a 2018 historical floor, included-plan cost checks, candidate training, verified publication, and atomic activation. Read current membership from `datafetching/watchlist.txt` and validate historical publications against their own saved universes.

September 14 onboarding: CROX, PATH, TWST and IONQ are now active, bringing the
current universe to eleven (264 forecasts, 264 stock-only intents, 33 production
OPRA cursors). The user authorized one late preparation tail for that date only;
`deadline-exception.json` binds its source, original deadline and 05:00 PDT
expiry. Reports preserve both original and effective deadlines. This exception
does not change future overnight deadlines or permit intraday model retraining.

A source-verified price path with explicit unavailable reference/pair status may
complete the informational trade plan with `UNAVAILABLE_PRICE_REFERENCES`, no
chronological trades, and no ending cash/holdings projection. Never fabricate a
price or relax the native source, account, ownership or live quote checks.
This supersedes requirements below to produce numeric projections at every
clock when the underlying observations are unavailable.

The desktop **Gameplan** tab validates the saved receipt, manifest and explicit
projection status before loading these plans. When the cash projection is
unavailable, it opens **All forecasts**, retaining company/horizon filters,
published probabilities, directions, windows and report access. Projected
trade counts and quantities show unavailable rather than zero or an inferred
hold. The notice names the companies with missing price references. This is a
read-only view of the saved publication and does not change the live trader.

The user's separate manual late-opening exception is
`--late-opening-date 2026-09-14`, valid only before 05:00 that day with the managed
Gameplan policy. Original forecast IDs/target ends and persistent entry-slot
claims remain unchanged. Do not add it to a Scheduled launch; later sessions
retain the normal 04:00 opening and 04:01 first entry.

This is the operating contract for the Scheduled overnight task and its health
watch. The task stays with fetching, training, and prediction while they run.
It investigates errors immediately, repairs verified defects, and resumes the
failed stage when a restart is needed. Long, healthy training is expected.

## Operating day

- The production universe is configured in `datafetching/watchlist.txt`; use
  [SYMBOL_ONBOARDING.md](SYMBOL_ONBOARDING.md) for additions. Validate each saved
  run against its own symbol manifest, including older six-symbol publications.
- The stock action window is 04:00 through 17:00 America/Los_Angeles. All configured
  symbols support the required extended-hours stock sessions.
- Heavy provider work, feature materialization, model fitting, assessment, and
  next-session planning run after the 17:00 stock close and before 04:00.
- “Current” overnight market evidence means the complete most recently finished
  market session. A final option quote can therefore be several clock hours old
  when the option market is closed and still be the correct newest observation.
- The stock universe's action clock extends to 17:00 PT, while standard listed
  equity options finish their normal executable session at 13:00 PT. Thus the
  September 3 OPRA close is `2026-09-03T20:00:00Z` even though stock extended
  hours continue afterward. Those clocks are intentionally not conflated.
- Quote age at overnight planning is not an order-time quote check. Any future
  live executor must revalidate the exact frozen legs against a current tradable
  quote and may execute or skip only. It may not substitute a different contract
  or rebuild the plan intraday.

Single-owner command:

```powershell
cd C:\dev\ducketz
.\.venv\Scripts\python.exe -u -m ml.overnight_runtime --datastore-target pc --once --scheduled --stock-only --independent-stock-horizons --stock-price-source xnas-itch-archive-v1
```

The command has no order authority. The fetch stage may read Schwab market data.
No overnight stage can place, cancel, or replace an order.
The Scheduled task wakes daily at 21:05 America/Los_Angeles. The native scheduled
mode checks the XNYS calendar after the system's 17:00 PT action close; this is a
session eligibility guard, not the daily start time. A weekend or holiday writes
a checksum-bound `NOOP_NON_SESSION_DATE`
receipt beneath `ml/overnight-runs`, runs no stage, and preserves the prior
gameplan pointer. A premature wake on an actual session fails closed.

## Sequential stages

The base stock-and-options command supports these stages in order. The active
stock-only XNAS schedule inserts stock history before evaluation, omits the two
options Strategy stages, and includes independent enrichment and account-aware
trade planning after publication, followed by the completed session's actuals review.
Each selected stage stops on failure:

1. `loop_a_close_fetch` — one Loop A close-cycle fetch, including the bounded
   production OPRA history owner.
2. `loop_b_directional_generation` — one complete Directional Loop B generation.
3. `stock_target_history` — for explicit `xnas-itch-archive-v1`, refresh the
   completed-session XNAS.ITCH minute archive. Native preflight must quote $0
   before acquisition; committed raw and normalized evidence is verified.
   Historical is the default. If its advertised range excludes a required
   recent session, the stage automatically attempts bounded XNAS.ITCH Live
   replay for that session's exact 04:00–17:00 Pacific window. This is permanent
   behavior in the native stage, including scheduled runs and failed-stage
   resumes; no one-off script or additional launch flag is needed.
4. `gameplan_evaluation` — evaluate all saved Gameplans using the refreshed
   outcomes from each publication's original price-source contract.
5. `strategy_profit_training` — train and assess the Options Strategy
   profitability models for `1h`, `4h`, `1d`, and `1w`.
6. `strategy_generation` — generate the exact options-strategy candidates from
   the new Loop B and Strategy-model authorities.
7. `gameplan_publication` — train the overnight path models and atomically freeze
   the next action date's stock forecasts, source-bound training cohorts and
   options intents.
8. `stock_enrichment_training` — for independent stock horizons, fit separate
   sizing models from the four immutable Gameplan cohort outputs. Verify the
   pinned source on resume, and preserve the original publication deadline.
   These models remain research/shadow when unqualified; the explicitly selected
   fixed-budget stock strategy does not depend on learned sizing promotion.
9. `gameplan_trade_planning` — for independent stock horizons, publish the
   reviewable capacity, direction-based quantities and planning prices from the same pinned
   Gameplan, a timestamped read-only account/holdings/working-order/quote
   snapshot, and a pure chronological cash/stock projection. This required tail stage
   writes a separate immutable trade-plan artifact; it does not submit orders,
   change live controls, reconcile or reserve horizon allocations, or modify
   the published forecasts or model statuses.
10. `gameplan_actuals_review` — after the successor Gameplan and trade plan are
    complete, compare the prior exchange session's saved Gameplan with the
    refreshed stock prices. Publish a separate results review and a dated link
    from the successor's Gameplan. This stage performs no fetch or training.

Stages do not overlap and do not rely on intraday checksum timing between
independent recurring processes. The overnight run writes a stage report and
receipt beneath `C:\DATASTORE\ml\overnight-runs`.

New full independent-stock runs complete only after the actuals review. An explicit
`--stop-after` boundary and a resumed attempt's recorded `stage_order` endpoint
remain authoritative: an older attempt ending at publication, enrichment or
trade planning does not silently gain another stage. All post-publication stages reuse the
checksum-bound `enrichment_gameplan` source recorded in the overnight report.
A failed stage resumes from that stage through its recorded endpoint with the
original source and next-session 04:00 Pacific deadline; it cannot choose a new
current Gameplan or rerun successful training. A failed actuals review resumes
only the review, retaining the completed successor Gameplan and trade plan.

### Completed-session Gameplan results

The final `ml.gameplan_actuals_review` stage receives the pinned successor via
`--gameplan-run` and retains `--deadline`. It verifies the successor's completed
trade-plan receipt before reviewing the previous exchange session, including
weekends and holidays. The comparison selects the last verified original
Gameplan and matching trade plan saved before that session's 04:00 opening;
the latest Gameplan pointer may already point to tomorrow and is not the source
of yesterday's estimates. Other saved forecast versions remain in the existing
cumulative evaluation history.

The separate `ml/gameplan-actuals-review-runs/<generation>/Gameplan-results.md`
contains per-stock hourly tables (04:00 through 17:00) with the **saved price
range, saved midpoint, actual price, observation time, dollar/percentage error,
and whether the actual price was inside the range**. It also retains every
frozen forecast with actual start/end prices, price move, and direction result.
The range is the original planning estimate, never a retrospectively refitted
prediction. Legacy plans without saved estimates explicitly say so.

Prices come from each original Gameplan's verified stock dataset. The existing
five-minute observation tolerance and minute-completion rules apply, with
actual observation times retained. No missing price is filled or borrowed from
another provider. Outcomes stop at the completed session's 17:00 boundary.
Future windows remain pending; mature windows without endpoints await data.
For each missing endpoint, new reviews retain the closest observation on the
required side of the boundary, its actual timestamp and distance, and whether
verified native request ranges cover the full permitted observation interval.
Complete request coverage with no usable bar is an observation gap, not evidence
of an unfinished download or proof that no trade occurred. The readable review
states that distinction rather than implying another unchanged fetch will fix
it. These diagnostics do not widen the five-minute rule, supply a price, change
an outcome status, or alter an older saved review. A download or source change
requires its own evidence; another symbol's denser bars establish no coverage
for the missing symbol.
Raw-price direction accuracy excludes neutral, pending and missing outcomes;
the model's cost-adjusted target and Brier score remain separate fields. Market
prices are not broker fills and this review does not claim realized trading P/L.

Outputs include `forecast-results.parquet`, `price-results.parquet`, `report.json`,
a verified manifest and receipt, and `Gameplan-results.md`. The latest pointer
is `ml/gameplan-actuals-review-latest/run.json`. The dated reader at
`ml/gameplan-actuals-review-by-date/<action-date>/Gameplan-results.md` is linked
from the successor's Gameplan and is replaced atomically after verification.
The original Gameplans, estimates, quantities and trading controls are retained.

### Account-aware trade-plan review

The native tail invokes `ml.gameplan_trade_planning` with `--gameplan-run` set to
the verified pinned publication and `--deadline` set to the overnight deadline.
Its separate outputs live beneath
`C:\DATASTORE\ml\gameplan-trade-plan-runs\<generation>`:

- `trade-plan.parquet` — one row for every frozen forecast, with independent
  capacity, signed direction quantity, post-hour cash/shares and the separate
  preserved scheduled-entry preview;
- `report.json` — snapshot provenance, cash and holdings context, policy,
  aggregate allocation checks, and exact unavailable-input or quality reasons;
- `planning-price-path.json` — working prices for all 04:00–17:00 clocks, with
  historical sample evidence and wider stress ranges;
- `planning-reference-completion.json` and `synthetic-reference-bars.parquet` —
  manifest-bound evidence for the bounded planning-only prior-close completion
  policy, including the actual observation time and any synthetic minute rows;
- `direction-ledger.json` — chronological transactions, hourly balances,
  ending holdings and explicit conditional-fill assumptions;
- `Gameplan.md` — the human-readable review with both quantity columns,
  planning prices, hourly cash/holdings and the end-of-day forecast;
- `receipt.json` — checksum-bound completion evidence, with zero orders.

The separate `ml/gameplan-trade-plan-latest/run.json` pointer selects the review.
Planning preserves the source publication's `24 × N` forecast identity and the frozen
Pacific entry/expiry windows. **Projected Trade Quantity** remains the standalone
whole-share capacity for one opportunity under the full configured horizon
budget, current cash/exposure limits and upper working price. It is not summed
as simultaneous orders. The adjacent **Direction Based Trade Qty** is the
chronological plan: promoted Bullish P(up) >= 54% buys with available cash,
Bearish P(up) <= 46% sells eligible held shares, and Neutral is zero. The main
table displays BUY/SELL quantities, Plan action, Cash available after (range)
and Shares remaining. Non-entry gap/outlook rows show a dash.

The stage reads fresh literal cash, every configured stock balance,
working-order reservations, options/other exposure and current horizon
allocations. It never assumes a cached share count or silently adopts manual
holdings into the live ownership ledger. The pure scenario may sell unallocated
held stock, but cannot sell another horizon's protected shares or pending sells.
At each clock it processes bearish sales, remaining due horizon exits, then
bullish buys through one shared cash balance. Sales take shorter horizons first;
buys take highest probability first, then shorter horizon and symbol. Purchases
use full horizon budgets, conservative remaining cash/exposure and the initial
5% cash buffer. A neutral row adds no transaction; a prior lot may separately
expire at that hour. Every forecast row shows balances after the entire hour's
batch, matching the main 04:00–17:00 portfolio table. Event details show each
transaction's before/after balances once. Overnight and weekly lots whose expiry
is later remain held in the 17:00 end-of-day projection.

The working range is the observed prior-close-to-clock median price plus/minus
20 basis points, rounded outward to cents. It is a conditional fill assumption,
not a confidence interval, guaranteed fill or execution limit. At least two
same-source historical pairs remain required; wider historical 5th–95th
percentile ranges stay in the evidence for stress analysis. Cash low/base/high
uses upper/base/lower purchase costs and lower/base/upper sale proceeds, before
fees and taxes. Assumed earlier sales may finance later projected buys; missing
fills or changed broker cash require recalculation. The no-fill baseline keeps
starting cash and shares unchanged. The base case is not an expected return.

The user-approved planning-only completion policy permits a short trailing
gap in the exact prior exchange session's 17:00 Pacific reference. The complete,
verified source partition must cover through that boundary. If the last actual
minute close was observed more than five but no more than fifteen minutes before
17:00, the planner may carry that same session's close forward through the missing
minutes. The limit is measured from the actual bar's completion time, not its
start. No dual Historical/Live agreement is required. Incomplete acquisition,
invalid observations, a different session, or a gap beyond fifteen minutes cannot
use this exception.

Each derived minute has OHLC equal to the last actual close, volume zero and
`is_synthetic=true`. These are explicit no-trade planning assumptions, not proof
that no trade occurred or new provider observations. The evidence retains the
original source identity, actual close observation time, gap length and separate
17:00 completion boundary; filling never makes the actual observation newer.
Entry bands and every working-price clock use the same completed reference.
Synthetic rows remain separate from the observed archive and are not used for
historical sample pairs, model fitting, evaluation or actuals review. Their
five-minute boundary gates remain unchanged, as do live quotes and order checks.

New trade plans use `cash-aware-gameplan-trade-planning-v4`. When this policy is
enabled, planning derivations use `historical-entry-price-band-v2` and
`conditional-hourly-planning-price-path-v2`, with the completion artifacts bound
by the trade-plan manifest. The native price-source and forecast contracts are
unchanged. Legacy v1 planning derivations retain their original strict reference
behavior, and previously published artifacts remain immutable.

The scheduled-default long-only policy retains its confidence-weighted entry
preview in expandable details and in the data. The opt-in manual policy
`gameplan-direction-current-market-v1` consumes the same frozen directions and
recomputes each order from actual broker cash, holdings, pending orders and
current quotes. It may sell eligible unallocated manual shares through explicit
sell reservations; it never counts reservations as fills or spends unconfirmed
sale proceeds. **Planning price and cash ranges are estimates only: neither
range can reject an order for being above or below it.** BUY limits use the
current ask and SELL limits the current bid, with permitted tick rounding.
The hypothetical ledger is not copied into actual broker balances. Model approval remains a separate
requirement. This section describes the implemented publication contract;
completion of any generation still requires its own verified native receipt.

The user can manually launch [`Start-Gameplan-Trader.cmd`](../../Start-Gameplan-Trader.cmd)
once to enable both stock controls and start this policy. Before its supported
04:00 Pacific opening, `--wait-for-open` keeps the worker asleep with its process
lock and a local heartbeat every 30 seconds or sooner. There are no broker
calls, model checks, inventory operations or entry-slot claims before wake.
It reads the current publication after waking, with the first entry batch at
04:01 and no second click or prompt. Previous-evening/weekend/holiday starts
select the next supported exchange session, respecting DST and the current
half-day exclusions. A control switched off stops the wait. The unchanged
03:55 Scheduled launcher adopts a verified waiting Gameplan worker; its own
default fixed policy and schedule stay unchanged. See
[manual start details](INDEPENDENT_STOCK_HORIZONS.md#one-action-manual-start-and-automatic-wake).

Daily display labels count the completed source session as Day 1 and show actual
calendar dates: a September 8 source produces `Daily · Day 2 · Sep 09` through
`Daily · Day 6 · Sep 15`. The count follows exchange sessions, including holiday
and weekend skips. Weekly labels show Days 2–6 plus their start and expiry dates.
Existing route IDs retain their source meaning: D+1 is the next exchange session
after the completed source session. The main review shows prices without repeated reference qualifiers or
historical-sample/status columns; detailed source files remain available from
the footer. Current cash, shares, investment values and price times stay visible.

Quantities and prices use the recorded account and quote snapshot. Future execution still needs
its existing current-quote, cash, exposure, ownership, session, activation and
quality checks. The review stage cannot activate trading or turn research
sizing models into qualified models.

### Permanent stock history delivery fallback

The Live fallback changes delivery, not the price dataset: EQUS.MINI is never
substituted for XNAS.ITCH targets. It requires an already verified Historical
baseline, catches up older available sessions with Historical first, and checks
every missing XNYS session. A missing older session outside replay retention
still blocks. Weekends and holidays create no invented sessions. Authentication,
checksum, source-identity and model failures do not trigger this fallback.
Coverage is checked against the Pacific action close, including winter closes
after UTC midnight; a UTC date cursor alone does not prove session completion.

Each replay requires native subscription acceptance, exact requested start,
symbol mappings and a replay-completed control. It is bounded to 24 hours of
lookback, five minutes and 64 MiB per symbol/session. A rejected entitlement,
shortened retained interval, timeout, disconnect, empty or malformed replay
fails the stage. The existing endpoint-observation and model-quality gates still
apply; a complete replay does not guarantee bars or a qualified target at every
minute.

Replay's unmodified DBN and its delivery, normalized, manifest and receipt files
live separately beneath `market-data/databento/stock-session-replay/XNAS.ITCH`.
Readers re-derive normalized rows from verified native bytes and admit only the
declared action window. Historical cursors are not advanced by replay, and old
publications retain their own file checksums and source contract. Identical
same-source overlaps deduplicate; conflicting observations stop consumption.
Verified replay partitions are reused after a restart without a new subscription.

Historical requests retain their exact account-specific $0 preflights. Replay
also records the exact session's Historical quote but does not misrepresent it
as a Live quote. Live acquisition uses this deployment's existing Standard flat
subscription authority and requires native acceptance for XNAS itself. Generic
unit tariffs are informational and do not establish effective cost or access.
The fallback never buys or activates a plan/license, switches datasets on an
access rejection, or starts the retired recurring stack. A live-access failure
remains a specific blocker; Historical is tried first on the next native resume.
Provider failures retain safe categories for access denial, replay retention,
timeouts and connection failures without exposing credentials or provider URLs.

### Explicit stock-only preparation

For the September 8, 2026 action session, the user explicitly requested stock
training and predictions for all seven symbols and all four horizons; options
remain research/paper and need not be prepared for that session. Use
`--stock-only` on `ml.overnight_runtime` for this scope. It omits
`strategy_profit_training` and `strategy_generation`, records the omitted stages,
and forwards `--stock-only` to `ml.nightly_gameplan` for publication. A new run
still performs the normal fetch, Directional generation, and cumulative
evaluation stages. This is an explicit preparation scope, not a remedy for
failed options calibration or a claim that options training succeeded.

The publisher trains the normal `1h`, `4h`, `1d`, and `1w` stock model groups
using all normally admitted features and unchanged calibration, assessment,
promotion, and deadline checks. Completed-session OPRA freshness still applies
because stock features can use options history; options profitability training
and candidate generation are not dependencies of stock-only publication.

With `--independent-stock-horizons --stock-price-source xnas-itch-archive-v1`,
target labels use the verified XNAS archive exclusively and retain the native
five-minute observation boundary rule. Identical archive overlaps are deduplicated;
conflicting prices fail closed. EQUS.MINI feature/continuation data is not silently
substituted for XNAS target outcomes. Forecasts, reports, and the four
`training-cohort-{horizon}.parquet` outputs carry the source identity. Historical
Gameplans retain their own source and evaluation contract.

New independent publications use `independent-gameplan-prior-session-features-v1`
for historical and current feature selection. A full hourly source bar must
belong to the immediately preceding exchange session, begin at or after its
regular open, and finish between its actual regular close and 17:00 Pacific.
Its recorded information and decision times must follow the bar end and be no
later than both the run cutoff and that session's 17:05 Pacific cutoff. This
admits the last regular-session features when extended-hours bars are sparse;
it does not require another model's next target to be 04:00. It never carries a
source across a missing exchange session or fills a target-price observation.

Source clocks, selected-session counts and the versioned selection policy are
bound into the Gameplan, model reports/payloads, forecasts and training cohorts.
The OPRA completion requirement continues through the full source session,
independently of an earlier selected feature bar. Retained models must match
the selection version. Old publications keep their original source selection
when evaluated, and the existing five-minute price-label rule is unchanged.
The correction is used by the next new preparation; do not rerun a completed
night or rewrite its immutable Gameplan merely to apply it.

The frozen grid remains 24 forecasts per symbol (264 for eleven). The matching
`24 × N` options rows are explicit `NO_TRADE_STOCK_ONLY` placeholders with no option
legs, candidates, profit probabilities, or Strategy source authority. The plan,
manifest configuration, and receipt identify `preparation_scope: STOCK_ONLY`.
These placeholders preserve the immutable table contract without preparing
options decisions. Verify their counts and empty option execution fields along
with the usual stock-model reports, receipt, provider coverage, and zero orders.

For an existing failed attempt, follow the same supervision and native recovery
procedure below, then add `--stock-only` to `--resume-run`. Resume retains its
verified completed stages and original deadline. When the failed stage is an
omitted options stage, it proceeds to stock Gameplan publication; it does not
rerun successful upstream stages. Candidate onboarding also requires the same
candidate watchlist and subsequent enrichment, validation, and activation steps
in [SYMBOL_ONBOARDING.md](SYMBOL_ONBOARDING.md#stock-only-candidate-continuation).

Stock-only publication and universe activation establish operational readiness;
they do not guarantee a trade. Model promotion, edge, and trading risk checks
still decide whether each stock signal can be used. Report whether a model
passed validation and which checks failed, rather than calling it "research-only".
Persisted model status identifiers and validation criteria stay unchanged.
The deployed independent stock design uses separate
holdings, exact entry/expiry windows and explicit `fixed-horizon-budget-v1`
sizing. The scheduled worker starts at 03:55 Pacific. Optional learned return
and sizing models do not determine the current Gameplan quantities or the
fixed-budget worker's sizing; their validation failures do not block those
quantities. Only the optional `qualified-enrichment` strategy requires them. See
[Independent stock horizons](INDEPENDENT_STOCK_HORIZONS.md). The default
stock-only scope retains the original five daily forecast slices.

Independent directional qualification also requires fitted history for the exact
symbol and route; another entry clock or later daily outlook cannot supply that
claim. The optional `qualified-enrichment` strategy additionally requires its
own held-out horizon quality and observed symbol/route/duration/source support.
Its unqualified models cannot enter through that strategy. The selected fixed
strategy instead uses the qualified stock probability directly for the explicit
capital rule `min(0.5, max(0, 2*p - 1))`, within the existing 1:2:3:4 horizon,
symbol, single-order and shared account limits. It requires at least 0.54 for a
long entry and creates no synthetic learned-return or profitability output.

September 8, 2026 selected deployment: current XNAS Gameplan
`20260908T093314.374067Z` has 168 forecasts, with all seven stocks and all 133
execution windows genuinely promoted. The daily model retains the verified
same-day champion from `20260908T085844.073361Z`; the failed challenger remains
research. The native $0 backfill completed 35 chunks and approximately 1.79
million minute rows. Source identities and promotion gates remain enforced.

Enrichment `20260908T092028.062383Z` remains research with zero qualified scopes;
it is nonblocking for the explicitly selected fixed-budget strategy. All 133
current execution forecasts are bearish or neutral, so there are zero bullish
entry opportunities. A ready worker should submit no BUY until its verified
forecast supplies a qualifying signal. Broker/ledger verification found zero
working orders and zero owned horizon allocations; manually held one-share
positions in the original six symbols remain untouched. The existing overnight
and health-watch schedules continue, and the stock worker starts at 03:55 PT.

## Active overnight supervision

`Loops Overnight Gameplan` wakes daily at **21:05 Pacific** (America/Los_Angeles,
including daylight-saving changes). It starts the entire Loop A close fetch and
the downstream stock-only workflow on an eligible completed exchange session.
Weekends and exchange holidays use the native no-op behavior. Before starting a
fresh pipeline, inspect existing work for that source session: leave completed
work alone, supervise a healthy owner, or resume an unfinished attempt only when
new availability or a verified repair resolves its failure. Resume preserves
completed stages, the exact configuration, and the original deadline.

The timing follows [Databento's XNAS.ITCH release table](https://databento.com/datasets/XNAS.ITCH):
Historical without a live license is normally released at the next Eastern
midnight, **21:00 Pacific**. The five-minute margin is not an availability
guarantee. Verify the account's actual required provider/schema coverage and
exact zero-dollar acquisition preflights. Do not retry the confirmed XNAS Live
license denial or substitute another price source when Historical is late.

It must remain active until the workflow completes or reaches an unresolved
failure. Starting a command and ending the Scheduled task is not completion.
`Loops Operations Watch` checks every 90 minutes, including weekends,
for a missed start, abandoned run, or failure that needs attention. Healthy work
continues across midnight, weekends, and exchange holidays.

The deadline is **04:00 Pacific on the next exchange session**. There is no short
per-stage timeout and no timeout merely because training is quiet. Friday
September 4 targets Tuesday September 8 because Monday is a market holiday.
The Friday run fetches Friday's completed session. Saturday reviews its outcomes;
there is no extra weekend market session to fetch.

Each stage writes an unbuffered `<stage>.log`. Every 30 seconds the owner updates
`stage-report.json` and appends `health.jsonl`: stage, PID and process creation
time, CPU time, memory, I/O, log growth, recent output/issues, and time left.
HGB/MLP fitting emits iteration progress, fit starts/completions, warnings, and
failures. Non-finite loss fails the fit. A warning needs inspection; it does not
automatically invalidate a model. Assessment/promotion criteria remain enforced.
Flat calibration emits an immediate `FIT_WARNING` and that model group does
not pass validation. This is a model-quality result, not a crashed training process:
inspect and report it without blindly restarting or weakening the promotion gate.
Target-boundary exclusions are reported before fitting, and assessment failures
report raw, calibrated, and training-base-rate scores immediately after scoring.
Investigate missing price coverage or model performance before restarting; an
unchanged rerun will not repair either condition.

Read progress without starting work:

```powershell
.\.venv\Scripts\python.exe -m ml.overnight_runtime --datastore-target pc --status
```

Before starting, adopting, stopping, repairing, or resuming work, generate one
new UUID for this Scheduled task and acquire supervision:

```powershell
.\.venv\Scripts\python.exe -m ml.overnight_runtime --datastore-target pc --claim-supervision <your-uuid>
```

Proceed only on `ACQUIRED`. `BUSY` means another task owns supervision; report
its current health without changing anything and end this wake. Reuse your own
UUID to renew the claim at least once a minute, including during tests and repairs.
The claim expires after three minutes without renewal, allowing the health watch
to take over if a Scheduled task disappears. Never reuse another task's UUID.
If renewal returns `BUSY`, stop making changes. Release your claim when finished
using `--release-supervision <your-uuid>`. This coordinates the human-readable
Scheduled operators separately from the Python pipeline's process lock.

The Scheduled operator must:

1. Follow the active process session and inspect status, new logs, and health
   history at least once a minute. Read errors while training runs, not only
   after its final exit. Rising CPU/I/O or log progress indicates work; a fresh
   heartbeat alone only proves that the supervisor is alive.
2. Investigate tracebacks, failed fits, non-finite loss, repeated provider
   failures, memory exhaustion, or missing heartbeats immediately. A possible
   stall needs several observations over at least ten minutes with no CPU,
   I/O, or log progress, plus examination of the stage/process. Quiet healthy
   training continues. Do not restart simply because a warning appeared.
3. Fix an established cause with a focused non-trading repository repair and
   relevant tests. Preserve concurrent changes. Record evidence, changed files,
   validation, and restart decisions in `operator-notes.md` within the run.
   Do not change trading code/controls, risk limits, promotion thresholds, raw
   market evidence, or immutable Gameplans to make an error disappear. Do not
   substitute cached output for required training.
4. If the running stage must stop for a repair, request a controlled stop:

   ```powershell
   .\.venv\Scripts\python.exe -m ml.overnight_runtime --datastore-target pc --request-stop-run C:\DATASTORE\ml\overnight-runs\<run> --reason "Specific observed failure and repair"
   ```

   Wait for the terminal receipt. If the supervisor has actually exited, use
   `--recover-run <run> --reason "Verified supervisor exit"` instead. Recovery
   checks process creation times, refuses a living owner or reused PID, stops
   only its remaining children, and writes a terminal receipt. Never delete
   locks or stop unrelated Python/UI/trader processes.
   If the exited owner already wrote a valid `FAILED` or `CANCELLED` receipt,
   recovery verifies that receipt and the process identities, then leaves the
   existing evidence unchanged before resume.
5. After the repair passes its tests, resume the failed stage:

   ```powershell
   .\.venv\Scripts\python.exe -u -m ml.overnight_runtime --datastore-target pc --resume-run C:\DATASTORE\ml\overnight-runs\<failed-run> --once
   ```

   Resume verifies the failed receipt/logs, retains completed stages and the
   original deadline, and works after midnight and over weekends. Monitor the
   resumed attempt too. Do not blindly restart unchanged failures. After two
   unsuccessful recovery attempts for one cause, report the unresolved blocker;
   continue investigating if a distinct, testable fix is available.
6. Verify the final receipt, current Gameplan checksums/next-session date,
   24 forecasts and 24 intents per configured symbol (168 each for seven),
   evaluation coverage, model assessments, zero
   overnight orders, and completed-session provider coverage. Report missing
   data and model groups that have not passed validation honestly, identifying
   whether a failed model is used by the selected strategy. For new full independent runs,
   also verify completed `stock_enrichment_training`,
   `gameplan_trade_planning` and `gameplan_actuals_review` stages, the separate trade-plan receipt and output
   checksums, its exact pinned Gameplan, 24 trade-plan rows per configured symbol,
   timestamped cash/holdings/quote evidence, and zero submitted orders. Verify
   the actuals receipt, previous-session date, original saved price source,
   forecast/price comparison counts and explicit pending/missing-data statuses. Preserve
   documented older or explicitly narrower stage boundaries.

The health watch checks current progress and the supervision claim first.
An active claim prevents a second operator from starting or repairing that run.
After the claim expires, the watch may acquire its own claim, adopt the existing
run, and use the same repair procedure. The process lock also prevents simultaneous
pipelines. The watch may start missing fresh work only after **21:15 Pacific** on
an eligible exchange date, at its next scheduled wake, leaving the 21:05 daily owner
time to start. Before then, only continue authorized unfinished work with a valid
deadline. After midnight use the existing attempt's resume path. Completed runs and holiday
no-ops do not trigger retraining. No active run means no new weekend fetch or
training job should be invented.

An explicitly started symbol bootstrap is tracked separately under
`state/symbol-onboarding`. The Health Watch checks that registry as well as the
overnight status; continuation of a registered attempt follows
[SYMBOL_ONBOARDING.md](SYMBOL_ONBOARDING.md#health-watch-continuation), including
the same supervision claim, candidate universe, and provider freshness gates.

Completion, stage failure, launch failure, controlled stop, and deadline expiry
write final receipts tied to the stage report and logs. A hard process exit is
detected through stale health and finalized with recovery. A missed 04:00 deadline
is reported and does not authorize intraday replanning.

## Saved Gameplan evaluation

The first Gameplan date is **September 4, 2026**. Every receipt-verified plan from
that date remains saved, including older plans after the latest pointer advances.
The identity is saved run plus forecast ID, so repeated editions do not collide.
Already evaluated forecasts keep their original score and scoring time. Missing
or corrupt saved evidence is an explicit error.

`ml/gameplan-evaluation-latest/run.json` selects a checksummed cumulative
`evaluations.parquet`, `summary.json`, and `review.md`. Each forecast is:

- `PENDING_MATURITY`: its exact target window has not finished.
- `MATURE_AWAITING_DATA`: its window finished, but its outcome is unavailable.
- `EVALUATED`: the actual outcome and probability/direction scores are saved.

Both pending states are revisited. Evaluation follows refreshed data and also
runs before Gameplan fitting; its result survives a later training failure.
Saturday at 09:00 uses the same evaluator without fetching or fitting. The first
review covers September 4 only and retains longer forecasts until they mature.
Earlier prediction systems are not review inputs. On September 4, 1,102
unreferenced earlier prediction runs were moved out of active folders into
`C:\DATASTORE\retired-predictions\20260904`. Historical market data and
source records still needed by retained models, Gameplans, or execution evidence
remain available. Permanent deletion was blocked by automatic approval review;
retirement is reversible.

## OPRA production-history contract

Loop A owns one incremental update for every configured symbol and
these three production schemas:

| Schema | Overnight use |
|---|---|
| `definition` | Point-in-time contract identity and terms |
| `cbbo-1m` | Exact historical option entry/exit BBO and execution evidence |
| `ohlcv-1h` | Option-surface/history context and cross-checks |

The cursor's `completed_through` date is exclusive. For example,
`completed_through=2026-09-04` proves the September 3 session was fetched. The
overnight plan refuses publication unless all `3 × symbol_count` cursors cover the
most recently completed session required by the action date. Other retained
OPRA schemas are research history and have no production freshness promise.

If Historical OPRA publication is delayed, the same Loop A history owner can
use finite, verified Live replay for the missing completed exchange session.
It requests explicit retained bounds, saves the native stream including
acknowledgement/completion/mapping records, and publishes a separate immutable
`live-session` segment. Definitions and hourly bars retain the complete UTC-day
request; minute CBBO covers one hour before regular open through the next UTC
midnight. The segment records its exact scope, and Strategy readers verify it
covers the entire regular session. No partial replay, `start=0` snapshot, error,
or missing completion can advance a cursor. Replay cursors carry checksum-bound
source coverage and cannot be regressed by delayed Historical metadata. See
[the Live replay fallback contract](../datafetch-ml/options-opra-history.md).

For candidate construction, “current overnight quote” means the newest
completed options-session snapshot known before the next action window. The
selector does not require that snapshot to postdate later after-hours equity
bars. It prefers the exact canonical OPRA snapshot when providers share the
same market timestamp, records the quote's real age at planning time, and never
treats that snapshot as next-session execution authority.

OPRA remains the bid/ask authority. If a verified Schwab snapshot describes the
same market timestamp, the planner may fill only OPRA fields that CBBO does not
publish—underlying reference, open interest, volume, and Greeks. It never
replaces the OPRA bid, ask, contract identity, or quote timestamp.

Strategy-profit outcome construction uses the nearest causal `cbbo-1m` snapshot
at each historical entry and exit boundary wherever that archive exists. `1h`
training requires exact CBBO. For older `4h`, `1d`, and `1w` targets beyond the
shorter CBBO archive, a conservative `ohlcv-1h` fallback preserves the longer
history and is explicitly labeled modeled rather than executable BBO. Exact and
modeled evidence remain separate features/counts.

## Frozen forecast grid

Each completed overnight publication contains exactly 24 forecasts per symbol:
`24 × symbol_count` total, or 168 for a seven-symbol publication. The same count
applies to options intents. Older six-symbol publications retain their original
144 rows in each table.

| Model group | Routes per symbol | Anchors |
|---|---:|---|
| `1h` | 14 | 04:00 through 17:00 inclusive |
| `4h` | 4 | 04:00, 08:00, 12:00, 16:00 |
| `1d` | 5 | D+1 through D+5 |
| `1w` | 1 | one direct five-session forecast |

The intraday route suffix is the predicted checkpoint at the **end** of its
target window, not the time at which a reader should consume an already-matured
forecast. For example, `1h@06:00` predicts 05:00–06:00 and is consumed at
05:00. At 04:00 the reader records the two precomputed opening-gap checkpoints
and also consumes forward `1h@05:00` and `4h@08:00`. At 08:00 it consumes
`1h@09:00` and `4h@12:00`; at 16:00 it consumes `1h@17:00`. The 17:00 wake has
no new forward intraday window and records session close only.

Starting with the v2 planning contract, every intraday forecast and matching
option intent stores both `forecast_anchor_local` (target endpoint) and
`action_anchor_local` (first dispatch time). The reader validates the explicit
action field; its route-derived mapping exists only for the immutable v1
compatibility artifact.

The 04:00 anchor is the start of this system's stock action day. It does not
change the exchange hours during which an equity option can actually trade.
Options intents can be planned for every route, but a route outside a tradable
option session cannot become an option order until its exact frozen legs pass a
live quote/session check.

An intent is not a trade merely because both models exist. The paper-entry gate
also requires a promoted direction model, at least 0.05 direction edge from
0.50, a calibrated option-profit probability of at least 0.55, positive modeled
net profit and return on risk, matching delta direction, completed-session BBO
quality, and a complete listed-options execution window. Because route labels
are target endpoints, the 1-hour option-compatible checkpoints are 08:00
through 13:00 PT (entry 07:00 through 12:00), and the option-compatible 4-hour
checkpoint is 12:00 PT (entry 08:00). Other stock forecasts remain in the
immutable grid but their option intent is explicitly `NO_TRADE`.

For each model group, the overnight builder trains both a histogram-gradient
model and an MLP neural-network challenger, compares these, regularized logistic
candidates and fixed blends
on a later chronological selection partition, calibrates on a separate
partition, and reports final performance on an untouched assessment partition.
Assessment failure leaves that group's output explicitly research-only; it is
never relabeled as promoted.
For new independent-stock publications, the user-approved v2 operating policy
allows Brier score up to training-baseline + 0.005 and log loss up to baseline
+ 0.01, while retaining probability variation, calibration and assessment sample
checks. Approval under these tolerances does not assert measured baseline
outperformance. The daily logistic grid uses C = 0.001, 0.01, 0.1 and 1, selected
strictly on development data before final assessment. Numerical results, the
policy version and tolerances are recorded and verified consistently by the
publisher, reader and champion selection. Older strict-policy publications
remain unchanged. Investigate and correct actual training/data defects when a
candidate fails, then rerun the affected stage; do not repeatedly select
candidates using final assessment outcomes.
Promotion also requires varying calibrated probabilities on both the calibration
and assessment partitions, with both target classes available. A constant
base-rate fallback cannot pass as a promoted directional model. The report saves
the calibration status, slope, positive rate, and probability ranges. Promotion
now requires lower Brier score **and** lower log loss than a constant probability
estimated from training plus selection rows. The former tolerances could promote
a model that performed worse than that baseline. Varying output alone is insufficient.

Intraday target contract `overnight-path-targets-v2` requires the observed prices
to be within five minutes of both stated window boundaries. Minute-bar closing
prices are observed at the bar's timestamp plus one minute. The same rule applies
to the prior close/current open used for opening gaps. Sparse interior bars are
allowed; a stale endpoint cannot stand in for a different forecast horizon.
Rejected labels are excluded from fitting and from new evaluation scores. Mature
forecasts without valid boundary data stay `MATURE_AWAITING_DATA` for later retry;
existing saved evaluations and frozen forecasts remain immutable.

Reports retain boundary timestamps, exclusion counts by route, bounded examples,
raw-versus-calibrated assessment scores, and changes in Brier score and log loss.
The [controlled four-hour diagnosis](audits/2026-09-04/FOUR_HOUR_CALIBRATION.md)
documents the label defect and separates its repair from evidence of predictive
quality. The existing raw-score UI display remains a display preference.

## Immutable publication

The atomic pointer is:

`C:\DATASTORE\ml\nightly-gameplan-latest\run.json`

It selects one immutable directory beneath:

`C:\DATASTORE\ml\nightly-gameplan-runs\<generation>`

The generation contains:

- `gameplan.json`
- `forecasts.parquet`
- `option-strategy-intents.parquet`
- `prior-gameplan-evaluations.parquet` (snapshot of all saved forecast evaluations)
- `model-reports.json`
- one fitted artifact per model group
- checksum-bound `manifest.json` and `receipt.json`

The publisher will not replace the current pointer after 04:00 for that action
date. This makes the day's decisions reproducible and allows the next 17:00 run
to compare every matured directional forecast with what happened without
intraday plan drift. The current evaluator does not manufacture realized option
P/L from an intent: that requires an exact-leg execution/revalidation receipt.
A future mark-to-market study of an unexecuted intent must be published
separately and labeled counterfactual.

## Daytime consumers

The bounded paper/advisory consumer is:

```powershell
.\.venv\Scripts\python.exe -m ml.gameplan_executor --datastore-target pc --once
```

It verifies the gameplan receipt, action date, 04:00–17:00 window, and exact
route. It writes a decision receipt under
`C:\DATASTORE\ml\gameplan-decision-runs`. It does not train, fetch, alter the
gameplan, import a broker client, or place an order. `orders_placed` is always
zero.

Live stock or option execution is a separate deployment decision and requires
explicit operator authorization. For options, the only permitted future live
transition is same-leg revalidation followed by execute-or-skip.

The deployed stock-only consumer is:

```powershell
.\.venv\Scripts\python.exe -u -m ml.gameplan_stock_trader `
  --datastore-target pc --execute --target-horizon all --sizing-policy fixed-horizon-budget-v1 --run-session
```

It requires two independent persistent controls (`CONFIRM_ACTIVE_TRADING` and
`CONFIRM_GAMEPLAN_STOCK_TRADING`) in addition to `--execute`, and then reuses
the established Schwab session, quote, cash, exposure, spread, sizing,
exact-once, deadline, and reconciliation controls. It never backfills missed
hours. Each horizon owns its shares and receives a portion of one shared
account budget. All due horizons enter one combined risk-limited batch; an
hourly signal cannot sell a weekly allocation. Live entries require the bounded
session manager so timed exits remain supervised. The old shared-position
adapter rejects independent target publications. Options remain non-executable.
The CLI default remains `qualified-enrichment`; this deployment explicitly
selects deterministic fixed budgets. Learned research status is preserved.

## Duckets forecast UI

The Duckets `Rolling Forecasts` tab now defaults to the checksum-verified
`ml/nightly-gameplan-latest/run.json` pointer whenever it exists. It does not
wait for, or activate, the paused daytime executor.

September 8 native adapter verification reports seven published symbols, no
pending symbols, 168 forecast rows and seven weekly snapshots. COST's obsolete
supplemental research/activation badge is cleared; actual model status remains
bound to the current publication.

- On initial load, the UI selects the frozen `1h` row whose target window is in
  progress, then the nearest future row or final completed row at the edges of
  the action day.
- The same rule selects the current `4h` window. Consequently the displayed
  independent route advances at 04:00, 08:00, 12:00 and 16:00. The 16:00 target
  ends at 07:00 on the next exchange session. Older target versions retain
  their original three forward windows and opening-gap context.
- The pulse grid shows one percentage per cell. When a group's calibration is
  flat, the UI displays its valid saved raw scores, with colors based on those
  scores. Other groups retain their published probabilities. Expanded cards
  use the same display values; Debug Details retains the raw/calibrated values,
  calibration diagnostic, and display source. This display choice changes no
  frozen artifact, promotion status, evaluation input, or trader input.
- The ordinary `1d` card shows D+1. The remaining-week card shows the direct
  `1w` prediction plus all five frozen D+1 through D+5 daily predictions.
- Every displayed route also shows its checksum-bound options intent: the
  frozen Strategy name, modeled profit probability when one exists, pricing
  source, and the explicit `NO_TRADE` or same-leg-revalidation reason.
- An immediate load is followed by wall-clock-aligned refreshes five seconds
  after each hour. A 4-hour change therefore uses the same refresh path; no
  second scheduler is required.
- Probabilities, target windows, promotion status, and route identifiers are
  read from the immutable plan. Research-only forecasts remain visible with a
  research warning and are never relabeled promoted.
- UI rotation is read-only. It cannot fetch data, fit a model, rewrite a
  forecast, load a broker adapter, or place an order.

If the gameplan pointer is absent, the legacy rolling-intelligence output
remains a compatibility fallback. If a pointer exists but is stale or invalid,
the UI reports that condition instead of silently substituting a different
authority.

## Scheduler ownership

- `loops-hourly-operations` is repurposed as `Loops Overnight Gameplan` and runs
  the single owner at 21:05 PT daily, with native weekend/holiday no-ops.
- The former standalone OPRA maintainer stays paused because OPRA maintenance is
  stage 1 of the overnight owner.
- The separate Strategy paper-ledger stays paused. The stock daily-adaptation
  schedule now runs `Loops Operations Watch` every 90 minutes for daytime and
  overnight supervision. Its missed overnight start threshold is 21:15 PT. The cumulative
  Gameplan evaluator owns matured directional evaluation; option-intent P/L is
  not claimed without exact-leg execution or separately labeled counterfactual
  evidence.
- All former Loop-B intraday stock tasks stay paused. The former hourly
  Gameplan task now starts one native independent-horizon session worker at
  03:55 PT on weekdays with explicit fixed-budget sizing. The separate 13:05
  task is paused; the worker owns that transition and the prescribed close
  exits. Owned exits precede entry batches at HH:01, except 13:06. It checks
  native forecast promotion and all source, control, ownership and risk gates.
  September 8 currently has zero bullish entry signals despite complete stock
  forecast readiness; it must not force orders or treat research sizing as
  promoted.
- The Saturday read-only operator review remains independent.

## Failure behavior

- A provider, cursor, receipt, model, row-count, or checksum failure stops the
  run and preserves the prior valid pointer.
- A closed market with no newer session is not stale by itself.
- A weekday exchange holiday is an audited no-op, not a reason to retrain on
  unchanged data or replace the next session's plan.
- A supposedly current overnight dataset missing part of the latest completed
  session is stale and blocks publication.
- No stage may start the old recurring stack, delete a historical partition,
  modify a broker account, or infer order authorization.

## First observed publication

The first complete generation is
`C:\DATASTORE\ml\nightly-gameplan-runs\20260904T105944.876700Z`. It froze its
inputs at 03:59:44 PT and contains 144 forecasts plus 144 option intents for
`2026-09-04`. The atomic pointer completed at 04:00:09 PT, nine seconds after
the desired boundary. No order was placed and every intent was `NO_TRADE`.

The timing miss exposed two corrected implementation details:

- completed-session inference now reads the last eligible OPRA snapshot and the
  latest matching Schwab analytical snapshot instead of loading all committed
  snapshots; historical training still reads the complete archive;
- the publisher now performs a second clock check immediately before pointer
  advancement, so a future run that finishes after 04:00 fails closed and
  preserves the prior pointer.

The generation's direction models promoted `1h`, `4h`, and `1d`; `1w` remained
research-only. Its options candidates used promoted `1h`, `1d`, and `1w`
profit authorities. The `4h` profit model remained research-only, so no fitted
4-hour option score was allowed to authorize capital. Research predictions and
failure reasons remain retained for later retraining and evaluation.

The later [four-hour calibration audit](audits/2026-09-04/FOUR_HOUR_CALIBRATION.md)
found that the direction model's 24 four-hour probabilities were all exactly
50%: its calibration fell back to a constant base rate. The original promotion
gate missed that condition. Future publications reject that promotion, and the
UI displays the saved raw scores when calibration is flat, with diagnostic
details available in Debug Details. The original Gameplan and its published
status remain intact for evaluation.

A pre-correction bounded-reader check at 04:08 PT wrote
`C:\DATASTORE\ml\gameplan-decision-runs\20260904T110818.182966Z`. It consumed
the frozen 04:00 endpoint routes, loaded no broker adapter, submitted zero
orders, and preserved every option decision as its recorded `NO_TRADE` state.
That check exposed that dispatching a route at its target endpoint can consume a
window that has already matured. The corrected reader now maps each action hour
to the next frozen forecast endpoint as described above, while retaining the
special precomputed opening-gap signals. The recurring daytime reader remains
paused. A corrected v2 follow-up receipt at 04:14 PT is
`C:\DATASTORE\ml\gameplan-decision-runs\20260904T111403.230433Z`; it consumed
the four intended 04:00 action routes (`1h@04:00`, `1h@05:00`, `4h@04:00`, and
`4h@08:00`), loaded no broker adapter, and submitted zero orders.

After explicit live-stock activation, the first forward live boundary ran at
10:00 PT and wrote
`C:\DATASTORE\ml\stock-trader-decision-runs\20260904T170009.829457Z`.
The risk engine selected and submitted one AAPL SELL limit for 20 owned shares
at $321.17; immediate reconciliation observed it fully filled at that price.
No option order path was enabled. Earlier September 4 boundaries were not
backfilled.
