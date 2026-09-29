# September 28 HYPER Paper improvement

The first review selected a measured calibration/weighting change, preserved the
complete previous run, and created a verified fresh one-for-one account mirror.
The recurring task is **Hyperliquid Paper Improvement**, configured for
**GPT-6 Astra / Ultra**, every three days at **14:00 America/Los_Angeles**.
The first cycle was executed interactively using the same maintained workflow.
Deployment and the recurring schedule are verified complete and active.

## Result preserved before reset

The previous experiment was `20260926T180818Z-701515-btc-mirror`, opened with
$44,083.99583107362 on September 26. Its final committed Paper equity was
**$43,667.71621322533** at **2026-09-28 21:43:10.982639 UTC**, a ledger loss of
**$416.27961784829**. The nearby actual Duckets accounts totaled **$43,919.15**.
The common-mark comparison put Paper **$251.40951505829 behind**, or
**0.5702965675 percentage points** over the shared opening baseline. It found
no non-funding ledger updates for any of the three actual accounts; source
observations were 40.1 seconds apart. These are timed observations, not atomic
exchange snapshots.

There were **671 fills**, **$511,349.98 turnover**, **$269.58 fees**, and about
**-$0.90 estimated funding**. The earlier detailed attribution found BTC, ETH
and HYPE collectively made $65.40 before $260.37 fees. Inherited ZEC positions
and their risk exits lost $209.27 before $9.21 fees. ZEC had no Qualified signal
trades; its 15 fills were risk-cap and stop actions. These causes support
investigating both probability stability and trading costs.

The complete independent closing replay reconciled all **8,580 cycles**,
**34,320 equity rows**, **671 fills**, **30 virtual transfers** and **191 funding
entries** against the unchanged opening. The archive retains both `_paper` and
`_models`: **3,358 files / 2,397,877,031 bytes**, all matching SHA-256 evidence.
Independent model research and older archives remain preserved.

- [Closing comparison](C:/DATASTORE/hyperliquid/_operations/paper-improvement/20260928-paper-improvement-01/closing-comparison.json)
- [Closing accounting replay](C:/DATASTORE/hyperliquid/_operations/paper-improvement/20260928-paper-improvement-01/closing-accounting.json)
- [Archive verification](C:/DATASTORE/hyperliquid/_operations/paper-improvement/20260928-paper-improvement-01/preservation-verified.json)
- [Preserved run](C:/DATASTORE/hyperliquid/_paper_archives/20260928-paper-improvement-01)

## Model change and evidence

Calibration regularization changes from `C=1.0` to **`C=0.1`**. The normalized
ensemble weights change from equal shares to **40% logistic regression,
20% ExtraTrees, 20% HistGradientBoosting, and 20% MLP**. All four families remain;
none was removed or replaced. The base estimators, MLP budget, chronological
70/15/15 split, horizon purging, qualification gates, Paper thresholds, risk
limits, quote execution and fee rates remain unchanged.

Five recipes were declared before fitting. Three earlier cutoffs selected one
candidate; two later cutoffs compared only that frozen selection to the
incumbent. Across eight confirmation cells, mean Brier changed
**0.251396 to 0.249851**, log loss **0.696360 to 0.692922**, and the target-change
proxy **0.079411 to 0.067841**, a **14.57% reduction**. Confirmation qualification
stayed four of eight cells; development qualification decreased four to three.
HYPE supplied most of the probability-score improvement; ETH/ZEC slightly
worsened. The proxy is not an execution backtest or fee-savings estimate.

The production implementation reproduced all twenty selected market/fold
assessments within **1.11e-16**. The study uses correlated, previously researched
history. Confirmation was reserved within this trial, not globally untouched.
The next Paper period tests the hypothesis prospectively; improved trading
performance is not established yet.

- [Research report and limitations](C:/DATASTORE/hyperliquid/_model_research/20260928T213400Z-calibration-weighting/REPORT.md)
- [Chosen change and incumbent provenance](C:/DATASTORE/hyperliquid/_operations/paper-improvement/20260928-paper-improvement-01/change-decision.json)
- [Fresh deployed models](C:/DATASTORE/hyperliquid/_operations/paper-improvement/20260928-paper-improvement-01/fresh-model-verification.json)

## New opening and lifecycle verification

Experiment **`20260928-paper-improvement-01`** opened at
**2026-09-28 21:45:32.770303488 UTC / 14:45:32 PDT**:

| Account | Verified opening equity |
| --- | ---: |
| Alex | $6,762.605590385651 |
| Jeremy | $5,504.493683253599 |
| Clear Pond | $31,653.88365853435 |
| Total | **$43,920.982932173596** |

All nine signed positions, collateral calculations and historical perpetual
entries matched retained public responses. The opening had one cycle, four
equity rows and **zero fills, decisions, fees, funding, transfers and P/L**.
The seed keeps separate opening-mark stop references. Real account requests
and quotes are sequential: Alex's source-to-opening valuation difference was
$0.594665 and Jeremy's was -$0.437885, with no balancing cash adjustment.

The initial source matcher caught overlapping concurrent account-read windows.
It now identifies one consistent hashed owner across all four read types and
rejects remaining ambiguity. Existing captured responses verified the same
opening without another provider read or reseed. A regression test covers the
overlapping windows. The comparison baseline check also reconciles collateral,
inventory and the quote-time P/L bridge rather than assuming displays captured
at different times must agree to a fixed dollar tolerance.

- [Independent opening accounting](C:/DATASTORE/hyperliquid/_operations/paper-improvement/20260928-paper-improvement-01/opening.json)
- [Raw-source reconciliation](C:/DATASTORE/hyperliquid/_operations/paper-improvement/20260928-paper-improvement-01/opening-verification.json)
- [Source association repair](C:/DATASTORE/hyperliquid/_operations/paper-improvement/20260928-paper-improvement-01/prepare-source-association-repair.json)

## Durable workflow

[PAPER_IMPROVEMENT.md](../PAPER_IMPROVEMENT.md) defines comparison, declared
candidate trials, model replacement rules, archive/prepare/accept/complete
stages, duplicate handling and handoff. The comparison collector uses the same
Duckets account service and saves public response evidence. Incomplete or
nonempty external-flow histories require classification before return ranking.
The lifecycle tool retains maintenance until verified trading health, unchanged
opening/source evidence and advancing Paper observations are present.

Operations Watch v12 now reads the durable accepted baseline; its existing
30-minute schedule and GPT-6 Luna / Extra High settings remain intact. The
improvement task may change simulated models and reset Paper under the user's
standing instruction. Operations Watch remains limited to health and permitted
recovery. Powder was inactive throughout this cycle and no real account was
mutated.

## Final completion

Maintenance completed at **2026-09-28 21:52:15.074981 UTC**. Paper PID **56824**
and models PID **77856** were verified by module, configuration, working directory
and creation time. Data PID **57004** continued throughout. Final health at
**21:51:59.463716 UTC** passed with no warnings and committed Paper advancement
to **21:51:31.343277 UTC**. The independent later accounting check retained the
same opening and reconciled eight cycles and seven fills. At that observation,
equity was **$43,884.2836304999**, including **$24.4547864755 simulated fees** and
subsequent market movement. This is distinct from the untouched $43,920.98
opening; ordinary startup signal/risk adjustments already incur costs.

The real post-start comparison also passed the new source-to-opening baseline
reconciliation, with no external flows. The verified accepted baseline is
`_operations/paper-current-accepted.json`; the append-only completion index is
`_operations/paper-improvement/completed/`. Both scheduled-task memories were
synchronized with the new baseline, preserved predecessor and evidence paths.

The app configuration was reread and confirms **ACTIVE**, **gpt-6-astra**,
**ultra**, local project `ducketz`, and a three-day 14:00 recurrence. The next
recurrence derived from the saved rule is **October 1, 2026 at 14:00 PDT**.
Operations Watch's active status, schedule, model, effort, project, workspace
paths and notification setting were verified unchanged. Local scheduling needs
the PC, app, project and network/model capacity available.

A [post-handoff health check](C:/DATASTORE/hyperliquid/_operations/paper-improvement/20260928-paper-improvement-01/post-handoff-health.json)
at **21:54:19.686318 UTC** passed with the same seed and verified workers, no
warnings, and further committed Paper advancement to **21:54:09.815395 UTC**.

Validation comprised 288 model/config/recipe/artifact/runtime tests; 503 Paper,
view, comparison and forecast/workspace tests; 18 final comparison tests
(including ten already in the 503 suite); and 14 lifecycle tests. One lifecycle
symlink test was skipped because Windows denied symlink creation privileges.
Independent live opening, raw-source, accounting, process, publication and
advancement checks passed. `git diff --check` passed.

- [Deployment receipt](C:/DATASTORE/hyperliquid/_operations/paper-improvement/20260928-paper-improvement-01/deployment.json)
- [Final trading health](C:/DATASTORE/hyperliquid/_operations/paper-improvement/20260928-paper-improvement-01/final-health.json)
- [Advancing accounting replay](C:/DATASTORE/hyperliquid/_operations/paper-improvement/20260928-paper-improvement-01/advancing-accounting.json)
- [New-run comparison verification](C:/DATASTORE/hyperliquid/_operations/paper-improvement/20260928-paper-improvement-01/poststart-comparison.json)
- [Active automation and watch-settings verification](C:/DATASTORE/hyperliquid/_operations/paper-improvement/20260928-paper-improvement-01/automation-verification.json)
- [Validation suites](C:/DATASTORE/hyperliquid/_operations/paper-improvement/20260928-paper-improvement-01/validation-suites.json)
