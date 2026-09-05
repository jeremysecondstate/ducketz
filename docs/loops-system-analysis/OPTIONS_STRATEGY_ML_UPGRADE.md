# Options Strategy ML upgrade

## Metric meanings

- **Direction Up (ML):** calibrated probability that the underlying finishes up
  over a stated route. It is not an option strategy's profit probability.
- **ML Profit Probability:** separately calibrated probability that one exact
  candidate has positive net profit after entry/exit BBO cash flows and fees.
- **Scenario Coverage:** heuristic fraction of local scenarios that are
  favorable. It is not a probability and remains labeled separately.
- **Expected Return:** a separate modeled candidate outcome, bounded by declared
  risk mechanics.

## Current four-horizon path

Strategy-profit training now covers `1h`, `4h`, `1d`, and `1w`. For each
horizon it trains a histogram-gradient baseline and an MLP neural challenger,
including fixed convex blends. Selection uses a later purged chronological
training sub-window. Calibration and assessment remain isolated; assessment is
never used to choose or fit the model.

The 4-hour evidence set contains only 65 independent windows with both entry
and exit inside the listed-options session. Its declared chronological minimums
are therefore 30 training, 15 calibration, and 15 untouched assessment windows.
The 1-hour split remains 60/30/30; daily and weekly remain 252/63/63. These are
independent target windows, not thousands of correlated candidate rows counted
as separate decisions.

Loop B's directional probability and direction alignment remain direct Strategy
features. Option-market feature families (`opt__` and `opx__`) also flow through
Loop B into both the profit model and the nightly path-model challengers.

## Historical OPRA outcome evidence

Loop A maintains these production dependencies after the 17:00 PT close:

- `definition` for point-in-time contract identity;
- `cbbo-1m` for exact entry/exit BBO economics;
- `ohlcv-1h` for historical option-surface context and cross-checks.

When `cbbo-1m` coverage exists, the outcome builder selects the first OPRA CBBO
minute at or just after entry and the last at or just before exit, each within
five minutes, with a causal one-minute underlying reference. `1h` requires this
exact path. Because the CBBO archive is shorter than `ohlcv-1h`, older
`4h`/`1d`/`1w` targets use conservative hourly-bar execution modeling calibrated
against overlapping exact CBBO. Those rows remain labeled
`MODELED_OPRA_OHLCV_1H`; they are never presented as exact BBO.

This matters especially for intraday horizons: one overnight decision may own
several distinct target windows, so target start/end are part of candidate
identity. Repeated decision timestamps across different frozen routes are valid;
an exact duplicate of the full identity remains a hard error.

## Freshness semantics

The trainer requires all 18 production OPRA cursors to cover the newest complete
session needed by the current Loop B samples. At night, the closing quote can be
hours old and still be the correct latest session evidence.

The selector compares the options-session clock with the options snapshot, not
with a later after-hours equity bar. For a shared snapshot time it prefers the
checksum-bound Databento OPRA chain, recalculates quote age at the overnight
cutoff, and accepts missing open interest/volume only when the direct two-sided
OPRA BBO and spread checks pass. This handles fields OPRA CBBO does not publish
without pretending they are zero or fabricating liquidity.

Planning freshness does not weaken execution freshness. The immutable gameplan
may select and freeze exact legs using completed-session evidence. A future live
executor must obtain a current tradable quote for those same legs and choose
execute or skip; it cannot switch strikes, expirations, or strategy families.

## Nightly outputs

The Strategy authority feeds `ml.nightly_gameplan`, which freezes 24 options
intents per symbol alongside 24 directional forecasts. Every intent records its
source Strategy generation, candidate identity, exact legs where available,
model basis, planning evidence time, and order-time revalidation requirement.

The planner does not equate model promotion with permission to enter. Its
frozen paper gate requires calibrated profit probability at least 0.55,
positive expected return and net profit, direction edge and matching delta,
completed-session quote quality, and an options-session-compatible anchor.
Failure of any one condition publishes a specific `NO_TRADE` reason.

The first deployment is advisory/paper-only. Model promotion is evidence of
offline gate performance, not a promise of profitability, and no historical
loss can be attributed to one missing feature without a controlled evaluation.
Each horizon is promoted independently. A rejected horizon remains recorded as
research evidence and publishes no fitted live score; it cannot prevent a
different horizon that passed its own untouched assessment from becoming
available.

## Observed correction

On 2026-09-04 UTC, the production-history catch-up completed all 18
symbol/schema scopes through the exclusive `2026-09-04` cursor boundary. That
includes the complete September 3 session. Training run
`ml/strategy-profit-training-runs/20260904T085857.373205Z` then assessed each
horizon independently. `1h`, `1d`, and `1w` passed and were atomically published;
`4h` remained research-only after its 15-window assessment was worse than the
training-base-rate benchmark and exceeded the calibration-error gate. A failed
`4h` assessment did not suppress the three independently valid models.

Strategy run `ml/strategy-runs/20260904T105214.742761Z` used the filtered
completed-session inference path and published 3,840 candidates. Every candidate
used a valid OPRA BBO from `2026-09-03T20:00:00Z`; every net delta was finite
after same-timestamp Schwab analytical enrichment. The earlier unbounded
inference attempt spent approximately 48 minutes loading history and was
stopped. The corrected path reads only the last eligible OPRA/Schwab snapshot
for inference; full history remains available to training.
