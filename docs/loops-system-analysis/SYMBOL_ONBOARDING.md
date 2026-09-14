# Adding a symbol to Loops

Research-selected additions use [Research symbol onboarding](RESEARCH_SYMBOL_ONBOARDING.md): an explicitly selected batch, a 2018 historical floor, included-plan cost checks, candidate training, verified publication, and atomic activation. Read current membership from `datafetching/watchlist.txt` and validate historical publications against their own saved universes.

`datafetching/watchlist.txt` is the production symbol list. `ml.universe`, stock
trader contracts, option routes, and the normal overnight fetch read that list.
The UI reads forecasts from the verified Gameplan and separately shows registered
unfinished onboardings. A candidate can have verified read-only directional
forecasts while its options Gameplan is pending; otherwise its probability
cells stay empty.
Each activated symbol adds 24 directional
forecasts, 24 options intents, and three production OPRA history cursors. An
eleven-symbol universe requires 264 of each forecast/intent and 33 cursors.
An older saved six-symbol Gameplan remains a
valid 144-row historical publication.

An explicitly selected stock-only Gameplan can complete stock onboarding
without options-model preparation. Its matching options rows are non-executable
`NO_TRADE_STOCK_ONLY` placeholders. See
[stock-only candidate continuation](#stock-only-candidate-continuation); the
historical forecast-only path below remains a separate read-only authority.

The single-symbol command below is `python -m datafetching.symbol_onboarding`;
the retained examples document the COST migration. Use
`python -m datafetching.research_onboarding` for new research selections under
the current independent stock-horizon workflow. The single-symbol helper keeps a
checksum-bound plan, provider results, completed request IDs, before/after byte
inventories, and activation evidence together in the selected output directory.
Existing Databento cold-start and OPRA writers retain their checksum checks,
canonical destinations, retry handling, and resumable batch jobs.

## Forecasts while options history is delayed

After the candidate's Loop A and Loop B preparation completes, a stock-direction
forecast can be generated without waiting for the latest OPRA session:

```powershell
.\.venv\Scripts\python.exe -m ml.directional_forecasts --plan artifacts\analysis\cost-onboarding-implementation\plan.json
```

This path uses the same 14 hourly, four four-hour, five daily and one direct
weekly targets as the ordinary Gameplan. It fits the same chronological HGB/MLP
challengers and calibration/promotion checks, excluding all `opt__` and `opx__`
features from both training and inference. Equity, fundamentals, macro and other
non-options inputs remain eligible. It retains the normal pre-04:00 Pacific
publication deadline; it does not backdate missing data or fabricate OPRA cursors.

Each immutable run lives under `ml/directional-forecast-runs`, with a per-symbol
pointer at `ml/directional-forecast-latest/COST.json`. The manifest and receipt
bind the registered onboarding plan, source generation, feature scope, models
and forecast files. Its authority is `READ_ONLY_FORECAST`. It cannot satisfy
Gameplan validation, activate a symbol, or replace the production Gameplan or
stock-model pointer. Forecast-only publications do not contain options intents.

Rolling Forecasts displays the candidate's cards and Prediction Pulse values,
with research/forecast-only labels and `Pending OPRA` in its options blocks.
Model gate results and any uncalibrated raw-score fallback remain visible. These
24 candidate forecasts are counted separately from the six-symbol/144-row
Gameplan. A verified full Gameplan containing COST supersedes this display.

Forecast-only outcomes are saved separately in
`ml/directional-forecast-evaluation-runs` and refreshed by the existing Gameplan
evaluation entry point. Exact future windows remain `PENDING_MATURITY`; no trade
or options P/L is inferred. Saved research forecasts remain available for later
evaluation after full onboarding completes.

### COST forecast-only run, September 6

Run `20260906T152335.670956Z` generated 24 COST forecasts for the September 8
action session, including the daily/weekly outlook through September 14.
All 111 eligible features are non-options; 43 options-derived input columns
were excluded. All four fitted model groups were `RESEARCH_NOT_PROMOTED`.
The 4h calibration was flat, so the UI's existing labeled raw-score fallback
applies; those raw scores are not validated probabilities. Evaluation run
`20260906T152402.401702Z` retains all 24 forecasts as pending maturity.

A direct Schwab check also succeeded: the COST chain returned 16 contracts with
Friday closing bid/ask and Greeks in a two-strike, 30-day probe. `/pricehistory`
returned 42 valid September 4 one-minute candles for `COST  260911C00915000`.
Those candles contain OHLC/volume, not historical bid/ask quotes. This confirms
useful partial options coverage, but does not establish a replacement for the
required OPRA definition/hourly/minute quote history. `fromDate`/`toDate` in the
chain fetcher select expirations; they are not a historical snapshot request.
Evidence: `schwab-options-capability-check.json` and
`schwab-option-candles-probe.json` in the onboarding evidence directory.

## Plan and fetch

Use the repository virtual environment. Inspect the current overnight status
and claim supervision as described in [NIGHTLY_GAMEPLAN.md](NIGHTLY_GAMEPLAN.md).
Do not change membership while a different overnight owner is running. Keep
the Health Watch active and renew the supervision claim at least once a minute;
its BUSY result prevents a second operator. Keep legacy tasks paused.

```powershell
.\.venv\Scripts\python.exe -m datafetching.symbol_onboarding plan COST --reference AAPL --datastore-target pc --output artifacts/analysis/cost-onboarding-implementation/plan.json
.\.venv\Scripts\python.exe -u -m datafetching.symbol_onboarding queue-history --plan artifacts/analysis/cost-onboarding-implementation/plan.json
.\.venv\Scripts\python.exe -u -m datafetching.symbol_onboarding fetch --plan artifacts/analysis/cost-onboarding-implementation/plan.json
```

Planning uses provider metadata without downloading market records. The reference
supplies retained OPRA and XNAS archive windows. The three production OPRA
schemas extend through the latest available Historical boundary. Shared CME
and macro history is reused. The operational bootstrap uses the existing
Databento, FMP, Schwab, and SEC fetchers, including Schwab price history and the
current option chain. Previous prospective snapshots cannot be reconstructed by
fetching a current snapshot.

`queue-history` optionally prepares independent long-range OPRA jobs at the
provider together, so one slow historical query does not delay submission of
the next. It reuses existing job IDs and the ordinary per-request submission
lock. The fetcher remains the sole canonical-data publisher; queued jobs do not
advance cursors or satisfy readiness.

`get_billable_size` is uncompressed billing data, not final stored GiB. Costs
come from `get_cost`, which applies subscription discounts. Matching a peer's
old retained dates can reach outside today's rolling Standard entitlement.
The COST scope quoted $0 for 24 requests and $0.012353420258 for XNAS imbalance
records from July 15 through August 5, 2026. The user explicitly authorized that
small extension. The included August 6 onward imbalance window quoted $0.
Review these values for each new symbol; the CLI has explicit byte and USD caps.

If interrupted, reuse the same plan and rerun `fetch`. Verified partitions and
batch jobs are reused; do not delete locks or edit cursor dates to force success.
The production watchlist remains unchanged during the bootstrap.

## Train, validate, and activate

```powershell
.\.venv\Scripts\python.exe -u -m datafetching.symbol_onboarding run-loops --plan artifacts/analysis/cost-onboarding-implementation/plan.json
$env:DUCKETS_PRODUCTION_WATCHLIST = (Resolve-Path artifacts/analysis/cost-onboarding-implementation/candidate-watchlist.txt).Path
.\.venv\Scripts\python.exe -m datafetching.symbol_onboarding validate --plan artifacts/analysis/cost-onboarding-implementation/plan.json
.\.venv\Scripts\python.exe -m datafetching.symbol_onboarding activate --plan artifacts/analysis/cost-onboarding-implementation/plan.json
Remove-Item Env:DUCKETS_PRODUCTION_WATCHLIST
```

`run-loops` first checks secondary Schwab price history for nonpositive or
nonfinite OHLC values. An unusable normalized series is preserved, with its
checksum and offending rows, under `quarantine/symbol-onboarding`; raw provider
deliveries and primary Databento bars remain intact. The quality receipt records
these exclusions. This prevents invalid secondary prices from entering returns
or technical calculations without inventing replacement prices. It then extends
native operational bars to the reference's retained first session and runs the
normal overnight stages under the candidate
watchlist: Loop A, directional training/prediction, cumulative Gameplan
evaluation, Strategy profitability training, strategy generation, and Gameplan
publication. Finally it trains the stock trader's enrichment model with the new
symbol indicator and validates the published outputs. The training report lists
mature outcome rows by symbol, including zero for a newly added symbol without
observed live outcomes. Such a symbol initially uses the shared fitted model;
its future evaluation windows remain pending until they actually mature. It uses an explicit
manual run, so a weekend/holiday onboarding is not mistaken for a scheduled
holiday no-op. No command above places orders.

For independent stock-model preparation while another source is still catching
up, set the candidate environment and run `python -m ml.stock_trader.training
--datastore-target pc --stage-only`. This trains and writes a verified generation
without advancing `stock-trader-model-latest`; it cannot satisfy activation's
current-model check. Finish the ordinary publishing training command after the
candidate Gameplan is ready.

The candidate override is inherited only by those subprocesses. Current
publication pointers advance when their normal publishers succeed; conduct
this maintenance outside action hours under one owner. If a supervised stage
fails, diagnose it and use the documented `--resume-run` command with the same
candidate environment, then finish stock-model training and validation. Do not
rerun successful stages just to retry an unchanged failed stage.

Activation requires complete history receipts, the candidate universe in the
verified current Gameplan, exactly 24 unique routes per symbol in both tables,
and a verified stock model with the candidate's feature. It atomically updates
the repository watchlist after these checks. A missing required OPRA session
continues to block publication; archive lag must not be disguised as fresh data.
Keep the final stock dry run non-submitting and leave all persistent trading
switches and model assessment thresholds intact.

Update the five active Scheduled tasks to read the configured list and validate
counts from the saved run's symbol manifest. Preserve schedules, notification
preferences, stock-only execution authority, and the paused legacy statuses.
The Saturday reviewer must evaluate each saved Gameplan's own universe and
retain longer forecasts from older six-symbol plans. Restore the Health Watch
and release the supervision claim when maintenance is finished.

### Stock-only candidate continuation

For September 8, 2026, the user explicitly clarified that all seven symbols
need stock training and predictions for `1h`, `4h`, `1d`, and `1w`; options
remain research/paper and need no preparation for that session. This scope
supersedes the earlier requirement to resolve options calibration before
continuing COST's stock publication. It does not mark the failed options fit
successful or change either model family's assessment gates.

After acquiring and while renewing the single supervision claim, inspect the
existing candidate attempt and verify its supervisor has exited. Recover it
through the native runtime before resuming. For the September 6 failed attempt:

```powershell
$env:DUCKETS_PRODUCTION_WATCHLIST = (Resolve-Path artifacts/analysis/cost-onboarding-implementation/candidate-watchlist.txt).Path
.\.venv\Scripts\python.exe -m ml.overnight_runtime --datastore-target pc --recover-run C:\DATASTORE\ml\overnight-runs\20260906T211429.688183Z --reason "Verified supervisor exit; explicit stock-only continuation"
.\.venv\Scripts\python.exe -u -m ml.overnight_runtime --datastore-target pc --resume-run C:\DATASTORE\ml\overnight-runs\20260906T211429.688183Z --once --stock-only
```

Run recovery only after the observed process state justifies that reason, and
resume only after recovery succeeds. This retains verified completed upstream
work and the original September 8, 04:00 Pacific deadline. The runtime records
the two omitted options stages and trains the normal stock Gameplan models with
all ordinarily admitted features. The 21 candidate OPRA freshness checks remain
required; no missing history is bypassed. Do not rerun `run-loops` or completed
fetch/Directional stages to continue this known downstream failure.

When supervised publication completes and its receipts pass verification,
finish the normal enrichment and native activation path with the same candidate
environment:

```powershell
.\.venv\Scripts\python.exe -u -m ml.stock_trader.training --datastore-target pc
.\.venv\Scripts\python.exe -m datafetching.symbol_onboarding validate --plan artifacts/analysis/cost-onboarding-implementation/plan.json
.\.venv\Scripts\python.exe -m datafetching.symbol_onboarding activate --plan artifacts/analysis/cost-onboarding-implementation/plan.json
Remove-Item Env:DUCKETS_PRODUCTION_WATCHLIST
```

Check each preceding command's successful receipt before continuing. Require
168 unique stock forecasts and 168 `NO_TRADE_STOCK_ONLY` options placeholders,
with no option legs, candidates, profit probabilities, or Strategy source
authority. Verify `preparation_scope: STOCK_ONLY`, the September 8 action date,
each model group's assessment, cumulative evaluation coverage, and zero orders.
Then complete a non-submitting stock-consumer check and record evidence and
storage in the onboarding directory. Preserve the six-symbol historical plans.

Activation means COST is in the operational stock universe. Stock entries still
require promoted direction models, sufficient edge, and existing risk controls.
Failed stock assessments remain research-only even after activation. The active
consumer now uses the independent four-horizon design, with separate ownership
and qualification checks. Its 03:59 session worker is scheduled but currently
returns a no-trade qualification result. See
[Independent stock horizons](INDEPENDENT_STOCK_HORIZONS.md). Default stock-only
preparation retains the existing 24-route immutable grid.

## Health Watch continuation

Starting `fetch` registers the exact plan under
`C:\DATASTORE\state\symbol-onboarding\<symbol>.json`. A plan that was merely
reviewed is not registered. An unfinished registered bootstrap is an existing,
explicitly authorized maintenance attempt; the Health Watch may continue it on
a weekend after acquiring its own supervision claim. This does not authorize
inventing a different universe, historical scope, or scheduled holiday run.

Read the registered plan and its `operator-notes.md`, progress, logs, lock owner
identities, batch-job states, and activation receipt. A living fetch/training
process with advancing provider progress or CPU/I/O should be supervised, not
duplicated. If its owner exited, use the existing idempotent `fetch` command with
the same plan. Never delete locks or rewrite cursor dates. An existing overnight
attempt uses the normal recover/resume procedure and retains its original
deadline; set `DUCKETS_PRODUCTION_WATCHLIST` to this plan's candidate file before
resuming it. Do not rerun completed upstream stages to retry a known downstream
failure.

If the required OPRA session is not available, query provider metadata on the
next Scheduled wake and record the observed boundary. Resume acquisition only
when that boundary advances enough to serve the session. A metadata check is
not a verified local-history receipt. The normal incremental writer and Loops
readiness checks must still pass. For COST's September 6 onboarding, the known
missing session is September 4, requiring an exclusive Historical end of at
least September 5. Do not force the expired Live replay window. Retain the
existing six-symbol production Gameplan/watchlist until all candidate gates pass.

When history is complete and its required session is available, finish the
candidate Loops stages, stock enrichment training, validation, activation, and
non-submitting trader/evaluation verification described above. Check the current
exchange deadline before publication. Record measured storage and completion in
this document and the onboarding evidence directory. Notify on completion or a
material failure/change, and remain quiet on unchanged provider lag. After an
activation receipt is verified, ordinary Scheduled ownership takes over.

## Evidence and storage

For COST, evidence lives in
`artifacts/analysis/cost-onboarding-implementation/`. `progress.json` distinguishes
total DATASTORE growth from paths owned by COST/COST.OPT. Total growth can also
include shared refreshes and model generations. `operational-history.json`
records the extra native-bar coverage. `validation.json` and `activation.json`
are success receipts, not placeholders; their absence means that stage has not
completed. All sizes use logical file lengths and 1 GiB = 1,073,741,824 bytes.

### COST execution status, September 6, 2026

The 25 approved archive requests finished at 09:25 UTC. The existing
Databento/FMP/Schwab/SEC operational bootstrap also completed with zero hard
provider failures. Every published archive manifest, raw file, normalized file,
and receipt passed checksum verification: 91,430,455 normalized records in total.
`archive-verification.json` records the checks. This completes the approved
available-history scope; the September 4 OPRA catchup remains outstanding.

The measurement at 09:25:56 UTC in `storage-measured.json` is:

| Component | GiB |
| --- | ---: |
| COST OPRA archive, including retained native batch staging | 2.506577 |
| COST XNAS equity archive | 0.060617 |
| COST operational fetched stock data | 0.008573 |
| Preserved secondary-history quarantine | 0.000284 |
| **Total fetched COST data** | **2.576052** |
| COST calculated features | 0.052120 |
| Shared ML generation growth across the seven symbols | 0.041944 |
| Other net growth | 0.000008 |
| **Total DATASTORE growth so far** | **2.670124** |

DATASTORE increased from 191,711,088,282 bytes (**178.544864 GiB**) to
194,578,111,692 bytes (**181.214988 GiB**). These are measured logical file
lengths, including retained source copies. They are separate from the quoted
6,915,217,432 uncompressed billing bytes (about 6.44 GiB) and from disk allocation.
The subsequent OPRA catchup and remaining model/publication stages add storage
beyond this dated measurement.
This is not a measurement of a fully activated seven-symbol stack. The earlier
estimates remain a dated planning record in
[SYMBOL_ONBOARDING_PROPOSAL.md](SYMBOL_ONBOARDING_PROPOSAL.md).

Completed preparation includes:

- Recovered seven-symbol Loop A: `20260906T083525.560558Z-pid61456`, with valid
  native operational history and zero failed COST technical/signal outputs.
- Directional generation: `ml/runs/20260906T084041.266342Z`, with all nine model
  groups newly trained, 219,267 final samples and 20,704 evaluations. COST has
  647 BACKTEST and eight LIVE prediction rows. The next holiday-shortened week
  has four daily slices; optional Option Pricing features remain subject to
  their existing coverage/freshness exclusions.
- Staged stock enrichment: `ml/stock-trader-model-runs/20260906T090205.483129Z`,
  with 27 features including COST. It uses 90 usable weighted mature outcomes;
  COST has no mature outcomes yet. The production model pointer was preserved.
- Cumulative evaluation: `ml/gameplan-evaluation-runs/20260906T085536.028629Z`,
  covering 288 forecasts in two older six-symbol Gameplans: 106 evaluated,
  174 pending maturity and eight mature rows awaiting price data. There is no
  retroactively invented COST Gameplan or outcome.

**COST remains a candidate, not an active production symbol.** Before the
20:54 UTC check on September 6, Databento OPRA Historical ended exclusively at
September 4, leaving the required September 4 session unavailable. That dated
provider blocker has now cleared; the execution update below records verified
acquisition and the current training blocker. The Health Watch remains ACTIVE
for the registered plan. The other Scheduled tasks retain their original statuses,
schedules, models, reasoning and notification settings; all five legacy tasks
remain paused.

#### Execution update, September 6 at 21:41 UTC

At 20:54:54 UTC, provider metadata returned a September 5 exclusive end for
`definition`, `ohlcv-1h`, and `cbbo-1m`. The normal COST-only incremental writer
completed with exit code 0 at approximately 21:13 UTC: all three scopes completed,
with zero failures, deferred scopes, or Live replay, 413,204,096 estimated
download bytes and USD 0 estimated cost. Native verification of the new
September 4 partitions found 3,712 definition rows, 8,554 hourly rows and
1,329,394 minute-CBBO rows. All 21 candidate symbol/schema cursors now verify
through September 5 exclusive. Evidence is in
`required-session-catchup-verification.json` and
`required-session-catchup-20260906T2056.log` in the onboarding evidence directory.

Completed candidate Loop A, directional generation, evaluation and staged stock
preparation were preserved. Supervised remaining-stage run
`C:\DATASTORE\ml\overnight-runs\20260906T211429.688183Z` failed at
21:41:48 UTC in `strategy_profit_training`. Its 4h calibration partition contains
1,168 rows across 15 clusters from June 29 through July 20, with every
`profitable` label equal to 0. The labels match the saved `net_profit > 0`
outcomes. The existing single-class calibration guard intentionally rejects this
partition, and its targeted test passed. OPRA freshness is resolved; calibration
support is now the blocker. Evidence is in
`4h-calibration-blocker-20260906.json` in the onboarding evidence directory and
the supervised run's `receipt.json` and `strategy_profit_training.log`.

The 1h model passed its existing score gate, but its top-ranked assessment
selections lost USD 667.97 in total, with mean return on risk of -3.167% across
30 decisions. The full 1h and 4h outcomes are retained; no complete training
receipt was published. Production remains six symbols, current Gameplan
`20260905T103409.421848Z` and the stock-model pointer are preserved, and COST
remains inactive. Strategy generation, the seven-symbol Gameplan, publishing
stock training, validation and activation have not completed.

For full stock-and-options preparation, continue only after an evidence-backed
change addresses this calibration support failure; unchanged retries or relaxed
gates are not a remedy. The explicitly requested September 8 stock-only scope
above can continue stock publication without those options stages. An exited
supervisor requires native `--recover-run` before `--resume-run`, with the same
candidate environment and original September 8, 04:00 Pacific deadline. Preserve
completed upstream work and the registered plan. Checkpoint reuse must follow
the existing runtime contracts, without custom artifact substitution. Final
non-submitting trader/evaluation verification and activated-stack storage
measurement remain outstanding.

At 21:44:38 UTC, the partial-state measurement in `storage-measured.json` records
DATASTORE at 194,673,099,299 bytes (181.303452 GiB), growth of 2,962,011,017 bytes
(2.758588 GiB) from the original baseline. Fetched COST data totals 2.616881 GiB,
including 2.547406 GiB of OPRA data; COST calculated features occupy 0.052120 GiB
and shared ML growth is 0.089568 GiB. Activation remains incomplete, so these
values do not describe an activated stack. The historical 09:25 measurement
above is preserved in `storage-measured-20260906T092556.json`.

The provider cause and Live alternative were checked directly at 10:04-10:06 UTC.
Databento's [incident report](https://status.databento.com/incidents/7wpzhhmrzgts)
attributes the historical delay to a carrier outage on its NY4-to-datacenter
link; Live was unaffected and no data loss was reported. Its stated processing
resumption is the next trading day, without an exact historical-availability time.
Live replay is a rolling retention window, not a guarantee of the most recent
market session; see the [replay documentation](https://databento.com/docs/api-reference-live/basics/intraday-replay).

The existing bounded capture route attempted COST `cbbo-1m` for September 4
12:30 UTC through September 5 00:00 UTC. The gateway rejected it with
`Invalid start time. Must be 2026-09-06T00:00:00Z or later, or 0`.
A separate diagnostic using `start=0` received subscription acknowledgement
and replay completion but zero COST quote records. An independent native-file
count confirmed only control and symbol-mapping records. This was a diagnostic,
not a publication with substituted start times. See
`live-required-session-check.json`, `live-all-available-check.json` and
`live-all-available-native-verification.json` in the evidence directory.
Earlier peer captures were still within retention: AAPL's September 4 quote
replay was saved on September 5 at 01:49 Pacific. Those saved records remain
valid after Live retention expires, but could not supply COST's then-missing
quotes. The later Historical catchup above resolved COST's missing session.

The Rolling Forecast layout and data contracts passed seven-symbol tests.
Registered COST onboarding first appeared with a status row and empty cells.
It now shows the independently verified research forecasts described above,
including a Prediction Pulse column and the supplied Costco logo. These values
do not contribute to the current six-symbol Gameplan counts. The first verified
Gameplan containing COST replaces the supplemental forecast display on
refresh. The logo is `app/ui/assets/security_marks/cost.png`, copied unchanged
from the owner's 512x512 `docs/logos-icons/costco-logo.png` on September 6, 2026.
Rolling Forecasts and Schwab Duckets both use this shared asset. The actual non-submitting
trader check correctly refused a Sunday
action against the saved Tuesday Gameplan; no broker state was captured and
no orders were selected or submitted. It does not constitute a COST trading run.

### Retained source-quality evidence

Schwab's long-range COST daily/weekly/monthly deliveries contained nonpositive
OHLC values in 2008/2009. Their normalized series were quarantined with raw
deliveries and checksums preserved; primary Databento and valid intraday bars
remain usable. The cause is unverified. See `secondary-history-quality.json`
and `schwab-price-quality.json`.

The existing OPRA writer excludes dates whose provider condition is not
`available`. Hourly batch files for June 3, 2024 and October 22, 2025, and daily
files for those dates plus June 18, 2021, are retained in batch state as ignored
provider files. The excluded dates account for all 9,580 daily records and
13,634 of the 17,170 hourly records in the differences from the broad preflight
counts (`provider-quality-reconciliation.json`). The remaining hourly difference
is 3,536 records, also the size of the minute-quote discrepancy described below;
its cause is unverified. XNAS long-range bar manifests preserve degraded-day
warnings for October 26, 2021 and September 19, 2022; daily bars also flag
July 7, 2021. Checksums establish delivery integrity, not perfect source quality.

One provider count discrepancy remains explicitly recorded: September 3 OPRA
`cbbo-1m` metadata reports 1,269,498 records, but both independent native
deliveries contain 1,265,962. The normalized partition contains all 1,265,962
delivered records with zero duplicate removals. All other daily minute-quote
counts match. See `quote-count-reconciliation.json`, `quote-native-count.json`
and `quote-redelivery-verification.json`; no missing rows were fabricated and
no canonical partition was replaced during this audit. Databento documents
some count approximation at non-ten-minute boundaries, but this whole-day
request does not establish that explanation for the observed difference.
See the [Databento metadata reference](https://databento.com/docs/api-reference-historical).

Targeted six-symbol, seven-symbol UI/trader, history/publication, supervision,
training and activation suites passed. The final activation tests also verify
that concurrent plans serialize changes to the shared production watchlist
and preserve its comments. Detailed test logs and continuation instructions
are in the evidence directory's `operator-notes.md`.
