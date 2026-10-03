# Hyperliquid system-reference changelog

This records changes to the maintained system contract. Runtime events and
performance history belong in their own journals and dated evidence.

## 2026-10-02 — Two-hour Paper cadence floor

The user changed future Paper Improvement rounds to one step forward and one
step back: a verified WIN adds one hour, a verified LOSS subtracts one hour, and
the minimum is two hours. Ties and unscored outcomes retain the committed
duration. Rule `win-plus-one-loss-minus-one-floor-two-v3` preserves all v1/v2
assessments and the consumed September 29 amendment; it does not reinterpret an
active v2 round. A receipt-backed transition during native `advance` verifies
the ending v2 assessment and fresh successor, then clamps the successor to two
hours before the app is reanchored. The Paper task uses GPT-6 Luna / Max; the
separate Operations Watch remains 30-minute, Luna/xhigh, and observational.

## 2026-09-29 — One step forward, two steps back

The user changed Paper Improvement to add one hour after a verified WIN and
subtract two hours after a verified LOSS, with a one-hour minimum. Ties and
unscored results retain the duration. Paper Improvement v4 and Operations Watch
v15 now describe this rule; Watch keeps its separate 30-minute schedule and
cannot score, apply penalties, tune or reseed.

The native, versioned policy amendment applies round10's completed four-hour
LOSS once to its existing round11 successor: two hours from its unchanged
opening, due September 30 at 12:50:48 a.m. Pacific. Historical assessments and
consumed ladder history remain unchanged. The app schedule is aligned to that
deadline, and normal seed-based reanchoring resumes after the next fresh mirror.
Models, trading settings, workers and ledger were not restarted or reset.
See the [rule-change audit](audits/2026-09-29-paper-loss-penalty.md).

## 2026-09-28 — Two-hour competitive Paper rounds

Changed the existing GPT-6 Astra / Ultra improvement task to start at two-hour
rounds. A verified after-cost win extends the next round by one hour; a loss or
tie retains its duration and triggers tuning. The persistent cadence helper
binds an immutable result to each experiment and advances once after a verified
fresh mirror; early, late or unrankable evidence cannot earn promotion.
This supersedes the three-day schedule and 60-hour guard. Operations Watch v14
retains its 30-minute schedule and narrow monitoring/recovery scope.

After the user confirmed Clear Pond spot purchases, archived the prior 5m run
with all638files verified and launched a new ten-position mirror at $43,877.58.
Kept the current four-family recipe for the first scored round. Native opening,
trading/accounting, health and completion checks passed; the first deadline is
September28 at21:20:36 PDT. See the
[competitive-round audit](audits/2026-09-28-two-hour-paper-ladder.md).

## 2026-09-28 — Five-minute forecasts and fresh Paper mirror

Activated the user-approved 5m OHLCV / next-5m h1 trial with 300-second fitting
progress, interval-aware dashboard/health/research and unchanged four-family
recipe and risk gates. Preserved the stopped 15m Paper/models and a verified
copy of its complete candle/coordinator history. The fresh nine-position mirror
opened at $43,937.02 with zero opening P/L or costs, and live append, forecasting,
retraining and Paper accounting passed the next five-minute cycle. Operations
Watch v13 and Paper Improvement v2 preserve the existing schedules/model settings
and retain five-minute history. See the
[launch audit](audits/2026-09-28-five-minute-paper-launch.md) for exact evidence,
subsequent trading costs and the October 1 review.

## 2026-09-28 — Three-day Paper improvement and first verified cycle

Added Hyperliquid Paper Improvement as an ACTIVE local Scheduled task, every
three days at 14:00 Pacific using GPT-6 Astra / Ultra. The workflow compares
Paper with the actual Duckets accounts, tests justified changes, requires
one-for-one replacement for removed deployed model members, archives completed
results, and verifies a fresh account mirror before resuming simulated Paper.
The native comparison and staged lifecycle tools preserve source evidence,
checksums, maintenance ownership and durable accepted-baseline receipts.
Operations Watch v12 accepts that handoff while retaining its prior scope and
30-minute GPT-6 Luna / Extra High configuration.

The first cycle preserved 3,358 files / 2,397,877,031 bytes. Closing Paper was
$43,667.72 versus actual $43,919.15, about $251.41 behind at common marks.
The new trial uses calibration C=0.1 and 40/20/20/20 logistic/ExtraTrees/HistGB/MLP
weights, retaining all four families and the existing split, gates and Paper
policy. A $43,920.98 nine-position opening passed zero-cost and raw-source
verification; fresh models and Paper were verified running with advancing
accounting. The next review is scheduled for October 1 at 14:00 PDT. See the
[first-cycle audit](audits/2026-09-28-self-improvement-first-cycle.md) for evidence,
test results, later startup costs and limitations of the candidate study.

## 2026-09-26 — Approved 70/15/15 training and BTC-inclusive fresh mirror

Activated chronological 70/15/15 fitting/calibration/assessment with strict
horizon purges and the existing four estimator families. Additional classical
and CNN/GRU candidates remain research-only. The exact-production split control
improved mean Brier on matched timestamps in three markets and worsened ZEC;
it does not establish after-cost profitability. See the
[expanded-model comparison](audits/2026-09-26-expanded-models-70-15-15.md).

Preserved the prior Paper/model trees with all 205 files matching their hashes.
Verified a fresh **$44,083.99583107362** opening with nine inherited positions,
including **0.2239149983 Clear Pond BTC**, and zero opening P/L, fills and fees.
Retained raw public-account responses independently reconcile quantities,
cash/collateral and perpetual entries. Subsequent strategy fills and their
$26.62 initial fees are recorded separately from the opening proof. The first
trading and advancing accounting/health checks passed. Maintenance completed
at 18:16:19 UTC and Operations Watch v11 accepted the baseline with its ACTIVE
schedule/model/project settings preserved. See the
[operational receipt](audits/2026-09-26-701515-btc-fresh-mirror.md).
**468 tests passed, one optional CatBoost test skipped.**

## 2026-09-26 — Bounded retuning and fresh verified mirror

Tested and rejected three model alternatives on chronological, purged data;
all worsened both selection probability losses. Retained and freshly fitted the
existing training recipe. Paper now requires 20% of target notional rather than
10% for ordinary rebalances, retaining $25 minimum, full-exit/risk bypasses,
shared49/51 signals, fees and risk limits. Preserved the previous Paper/model
trees intact; verified a new $42,081.21, nine-position mirror with zero opening
P/L, fills and fees, then resumed and independently checked execution accounting.
609 relevant tests passed. Operations Watch v10 uses the new baseline. See the
[retuning and fresh-run audit](audits/2026-09-26-paper-retune-fresh-mirror.md).

## 2026-09-26 — Bounded retries for forecast/book timing failures

Fixed a race where Paper fetched books before reading a newly published forecast,
then permanently consumed that forecast on a quote-only skip. Ticks now freeze
forecasts first, fetch fresh books with up to three bounded attempts, and recheck
forecast expiry afterward. Pending quote-only attempts persist across polling
and restarts. A narrowly verified historical cycle with three quote-error skips
and no monetary effects can finish through one append-only recovery key.
Completed/partial/mixed outcomes retain their deduplication protection.

Policy **1d260fb385aec9de** records execution version
`forecast_first_bounded_quote_retry_v1`. The current experiment, seed, 49%/51%
thresholds, fees, zero extra slippage and risk gates are unchanged. A graceful
worker reload recovered the13:15 ETH/HYPE/ZEC forecasts while valid: ETH/HYPE
filled; ZEC held for account capacity. Historical skips remain unchanged.
Independent accounting and history-prefix checks passed. **330 tests passed**;
Operations Watch now follows contractv9. See the
[timing/retry audit](audits/2026-09-26-paper-quote-retry.md).

## 2026-09-26 — Shared 49%/51% Paper entry and exit thresholds

At the user's direction, the active Paper experiment now uses
`entry_band=exit_band=0.01`: longs are eligible at or above 51%, shorts at or
below 49%, regardless of whether the position already exists. Longs exit below
51% and shorts above 49%. There is no entry/exit hysteresis gap. Equal bands are
now valid; shared-band conviction uses absolute edge divided by saturation to
give eligible boundary signals nonzero target size. Unequal historical policies
retain their existing sizing and hysteresis. Fees, zero extra slippage,
qualification, minimum trades, caps and stop cooldowns are unchanged.

Paper reads config at startup, so the worker was gracefully stopped and resumed
on the existing **20260926T123436Z-book-vwap** ledger. Opening, signed holdings,
cash, historical journals, stop references and the old policy were preserved.
New policy **747825fed0f17c1c** applies to new forecast publications; processed
forecasts were not replayed. **316 tests passed**, including shared-boundary
execution, exits and policy-change resume. Operations Watch **v8** records the
new policy and worker without changing the experiment baseline. See the
[policy-change audit](audits/2026-09-26-shared-49-51-paper.md).

## 2026-09-26 — Book-VWAP Paper fills without added slippage

Removed the configured two-basis-point fill-price adjustment at the user's
request. The active config and simulation defaults now use zero extra slippage.
Fills use fetched executable-depth VWAP, with separate configured taker fees:
0.045% perpetuals and 0.070% spot. Explicit historical nonzero scenarios remain
supported; prior fill evidence is unchanged. Strategy and risk rules are unchanged.

Preserved the preceding 12:00:59 run intact and prepared a fresh public-account
mirror, **20260926T123436Z-book-vwap**, opening at **$42,088.24361973616** with
nine positions and zero fills, fees and P/L. Subcent source-valuation differences
are documented rather than hidden through artificial cash adjustments. Resumed
Paper after opening verification. Its first seven fills equal raw book VWAP and
charged $26.9140638455 in applicable taker fees, with zero added slippage.
Independent replay reconciles cash, inventory, historical basis, risk references
and marked equity. **311 scoped tests passed** across market, policy, runtime and
ledger tests, including partial-depth buys/sells and distinct spot/perp accounting.

Operations Watch contract **v7** adopts policy `2c8197e2cbe140d6`, the new seed,
Paper config hash and worker identity. Data and models continued; Powder remains
inactive. See the [restart audit](audits/2026-09-26-book-vwap-paper.md).

## 2026-09-26 — Auditable fresh mirror before trading and separate stop references

Reconstructed the disputed 11:30:32 Paper opening independently. Its inventory
and equity reconciled, but seven executions within 0.66 seconds reduced the
inherited positions to dust and cost $40.902009: $28.268292 fees plus $12.633716
execution drag. Historical-entry stops, Qualified neutral/opposite signals and
sequential exposure-cap enforcement explained the adjustments. The first equity
journal point already followed a sale. See the
[forensic audit](audits/2026-09-26-paper-restart-reconstruction.md).

Fresh mirrors now commit an atomic opening observation before strategy fills,
with zero experiment P/L, fills and fees. A prepare-only lifecycle exports that
untouched mirror for verification; normal resume opens the same seed and uses
fresh market quotes without recreating the opening. H.Y.P.E.R. keeps the opening
baseline and signed inherited inventory visible alongside the current portfolio.
Public account-read windows and available exchange timestamps are retained.

Historical perpetual entry remains the accounting/display basis. A separate
persisted opening-mark reference makes the 3% stop measure the fresh experiment;
reductions retain it, additions weight it, and a new position starts at fill
price. Legacy positions migrate to their existing entry references, preserving
their prior stop semantics and immutable history. Qualified-only forecasts,
54%/46% entries, exposure limits, execution fees/slippage and uncapped model
training remain unchanged. Risk actions remain separately explained.

The user authorized a current-account mirror followed by trading, with a
temporary maintenance pause for the refresh. Preserved the disputed run intact
at `_paper_archives/20260926T113032Z-fresh-entry-4pp-disputed`, documented by
`_operations/paper-restart-20260926-preservation.json`; its unchanged eligibility
metadata is not acceptance of that baseline. The permanently excluded earlier
sample remains excluded and was not reconstructed.

New experiment **`20260926T120059Z-fresh-mirror-opening`** opened at
**12:00:59.251769781 UTC** with equity **$42,100.730843905156** and nine inherited
positions. Before trading, verification found one opening cycle, four equity
rows, zero fills/decisions/transfers/funding, and zero P/L/fees. The proof is
`_operations/paper-restart-20260926-opening-verification.json`. Policy ID is
**`6b404ac74a142e9f`**; configuration hashes are unchanged. Data and models stayed
running; Powder stayed inactive. Continuous Paper was launched after the
untouched opening was captured; post-start cycle verification is recorded in
the [completed opening/first-cycle audit](audits/2026-09-26-corrected-paper-mirror.md),
including independent cash/holdings/fees/marks replay and advancing worker checks.

**491 broad relevant tests passed**, followed by **80 focused UI tests**; these
are verification runs, not an additive count of unique tests. Regression coverage
includes opening atomicity/idempotence, historical basis versus fresh stops,
legacy migration, prepare/resume, real later costs, signal/risk behavior and
opening-chart/UI visibility. Updated Operations Watch to **contract v6** with
the fresh baseline, preserved disputed evidence and intentional prepare-only
stop handling. Automation prompt/memory are maintained through the app workflow.

## 2026-09-26 — Excluded prior sample and fresh 54%/46% mirror

At the user's direction, removed the 10:35:59 UTC Paper sample from active and
analysis locations. Automatic policy review initially blocked recursive deletion.
The user subsequently deleted the temporary cleanup directory, including the
checkpoint copy; `_pending_deletion/20260926T103559Z-excluded-paper` was verified
absent at **11:36:32 UTC**. The sample remains permanently excluded from analysis
and recovery. No new analytical archive was created. Independent model-comparison evidence moved to
`_model_research/20260926T103000Z-window-comparison`.

A new 1:1 public mirror opened at 11:30:32 UTC with $42,099.529977 and nine
inherited positions. Entry 54%/46%, Qualified-only models and existing risk
rules apply from the start. Updated watch baseline to contract v5. See the
[fresh-run record](audits/2026-09-26-fresh-entry-4pp-paper.md).
Deletion completion does not change that operating contract.

## 2026-09-26 — Broader Paper entries and recorded decision checks

Changed the active Paper entry band from five to four percentage points
(55%/45% to 54%/46%) under the user's forward-evaluation direction. Qualified
models, risk limits, costs and training cadence remain required; the existing
ledger/seed initially continued with a new policy ID. That sample was later
excluded under the fresh-run entry above; its outcomes are not evaluation data.

Fixed misleading generic Hold/Skip explanations. New decisions save exact
entry/exit, account, size, execution and cooldown checks; flat cooldown holds
now identify the actual blocker without changing retry behavior. The UI reads
those saved checks and uses only available historical facts for older rows.
See the [decision-gate audit](audits/2026-09-26-paper-decision-gates.md).

## 2026-09-26 — Bounded model comparison and Paper refresh

Archived the previous qualified-only Paper ledger and model history, ending at
-$45.46 P/L including $32.87 simulated fees. Tested two predeclared recent-fit
recipes with unchanged qualification thresholds. Both increased qualification
counts but worsened mean Brier/log loss; neither was promoted. Added a tested,
optional fitting-window cap with an uncapped default; active training settings
remain unchanged.

The subsequent 10:35:59 UTC Paper sample was later excluded at the user's
direction; its outcomes are not retained here as evaluation evidence. Model
research remains independently reproducible. **359 relevant tests passed** for
that implementation. See the
[comparison and refresh audit](audits/2026-09-26-model-comparison-and-paper-refresh.md).

## 2026-09-26 — Model retraining on each 15-minute candle

Changed the accepted model cadence from 3,600 to 900 seconds at the user's
request. Gracefully restarted only the model worker with an independent hidden
Windows launcher. Data and Paper owners continued; Paper's seed, baseline,
qualification gate and policy ID were preserved. Each new completed candle can
now trigger fitting; unchanged data does not trigger repeated fits.

Candidate-freshness display now follows the running model cadence (30-minute
stale threshold for the current 15-minute schedule), with the historical hourly
fallback for older status files. Removed the hard-coded hourly loading label.
Qualification thresholds, assessment/calibration partitions and immutable
forecast behavior are unchanged. More evaluations do not guarantee more
Qualified models or more independent evidence.

**215 relevant tests passed** across model configuration/runtime/artifacts,
the Paper view adapter and workspace. Verified all four new candidates,
accepted `retrain_seconds=900`, independent launcher ownership and advancing
Paper observations. See the [cadence-change evidence](audits/2026-09-26-model-cadence-15m.md).

## 2026-09-26 — H.Y.P.E.R. resize callback repair

Reproduced the reported Tk `pack`/`grid` conflict when returning from compact to
wide layout. Forecast, detail and policy panels now use `grid` in every layout,
with row/column sizing reset at each transition. Resize callbacks also tolerate
view destruction during idle processing; the layout cache updates only after
successful placement. Runtime workers and trading policy are unaffected.

The regression failed with the original traceback before the fix. **27 tests
passed** across the workspace and desktop mounting suites, including repeated
wide/compact/narrow transitions and destruction during resize. Workspace tests
now capture Tk callback exceptions so printed callback failures cannot silently
pass. Wide and compact offline fixture renders were also inspected.

## 2026-09-26 — Fresh qualified-only Paper experiment

Archived the entire gracefully stopped mixed-model experiment, including its
ledger, policies, funding cursor and logs, with source/configuration snapshots
and a SHA-256 manifest. Started a new read-only mirror seed at 07:42:57 UTC with
pooled opening equity **$42,071.6907597654** and all nine inherited positions.

Enabled `require_qualified_forecasts=true`. The new Paper recipe holds existing
targets without a valid Qualified signal, retains stop/cooldown/exposure-limit
reductions, and separates rejected model provenance from trade attribution.
Shared forecast validation corroborates the active eligible model and rejects
malformed forecast IDs before allocation. The previous mixed-mode fallback is
preserved when the qualification flag is false. Powder remains inactive; its
existing no-signal fallback was not changed.

Added explicit Paper fill reasons and No forecast/Risk exit labels to H.Y.P.E.R.
Updated operating documentation and watch recovery expectations for the new
seed and policy. **379 distinct relevant test cases passed** across the initial
regression selection and final reader/runtime checks. See the
[experiment evidence and limitations](audits/2026-09-26-qualified-only-paper.md).

## 2026-09-26 — Scheduled Hyperliquid Operations Watch

Created the separate local app automation `hyperliquid-operations-watch`, active
every 30 minutes using GPT-6 Luna / Extra High. Added the maintained
[watch procedure](OPERATIONS_WATCH.md), conditional recovery of unexpectedly
terminated data/model/Paper workers, explicit intentional-stop handling and
read-only Powder monitoring. Local PC/app availability is required; this does
not install an operating-system boot service or activate real trading.

Verified the saved scheduler configuration and ran the documented read-only
projection against the current installation. At 07:14:38 UTC, all three owners
were running and the Paper ledger observation had advanced to 07:14:32 UTC;
its original opening seed/baseline remained intact. The 643-second performance
export age was ordinary export-only lag. Powder remained unactivated. No
worker restart was needed for the watch setup. Checked Markdown paths/fences
and whitespace; the earlier independent-launch audit supplies recovery-method
evidence rather than a new failure-injection test.

## 2026-09-26 — Paper session recovery

Restored data, model and Paper workers after the user-confirmed Codex restart.
Validated the existing ledger and preserved its original seed/history. Relaunched
workers outside the Codex process tree through Windows, with fresh forecasts
and UI health evidence. No real trading or startup schedule was enabled. See
the [incident audit](audits/2026-09-26-paper-session-recovery.md).

## 2026-09-25 — Explicit Powder activation path

- Added the separate Powder execution runtime, strict verified mainnet broker,
  durable client-order intent/reconciliation ledger and user launch/check/stop
  commands. Every session requires explicit activation; no scheduler or `.env`
  change was installed.
- Shared validated forecast reading and pure allocation policy with Paper,
  adopted matching existing positions, enforced actual account collateral and
  shared ownership locks with manual submit/modify/cancel operations.
- Added separate read-only Powder observations/intents/actual fills to H.Y.P.E.R.
  Actual fee tokens are preserved; P/L/drawdown/funding totals and automated
  transfers remain unavailable pending complete cashflow implementation.
- Added the [activation/recovery runbook](POWDER_ACTIVATION.md) and updated the
  index, map, inventory, functionality, monitoring, UI and maintenance contracts.
  Test evidence and limitations are recorded in the runbook. Verification uses
  fake exchange responses and temporary ledgers, not live execution.
- Final regression selection: **917 passed** across all Hyperliquid tests and
  both workspace/app integration suites. Normal/compact Powder fixture images
  were inspected; local saved status reported **NOT_ACTIVATED**.

This entry supersedes the earlier disconnected-Powder implementation boundary.
The earlier dated screenshots and initial reference entry remain historical.

## 2026-09-25 — Initial system-analysis reference

- Established this index and operating reference alongside the existing
  stock/options [Loops reference](../loops-system-analysis/README.md).
- Mapped the independent data, model and Paper owners, their publication
  dependencies, the one-time mirror seed, evaluation evidence and read-only UI.
- Documented model qualification versus profitability, Paper opening-baseline
  accounting, transfers, account roles, execution assumptions and risk limits.
- Added process/source/timing diagnostics, control side effects and recovery
  guidance. Kept runtime observations separate from permanent architecture.
- Recorded H.Y.P.E.R. Paper/Powder boundaries and the current fixed UI market
  and horizon scope; Powder remains disconnected.
- Added a change-impact matrix, update checklist, evidence format and existing
  test-family map for future maintenance.

Baseline review used the working-tree implementation/configuration at revision
`f4efaeb` plus the existing component and UI implementation guides. Verification
for this documentation change is source review and local Markdown link/path
checking: 202 local links across the 12 new documents and six linked entry
pages resolved, with balanced fenced code blocks. No worker lifecycle,
strategy, ledger or scheduling change is part of
this entry. Previous UI screenshots and test results remain dated delivery
evidence, not newly measured runtime performance.

For later entries, record the behavior/configuration change, affected pages,
checks actually performed and any unresolved limitation. Do not claim automatic
documentation updates or replace old observations with current numbers.
