# Pooled causal sequence encoder and Loop C

## Authority boundary

This is a production-hardened **shadow and observe-only** lane. It is designed
to accumulate prospective evidence safely before any separate activation
proposal. It does not replace Directional Loop B, alter Options Strategies
ranking, synchronize an account, or expose a broker order method.

The immutable authority chain is:

```text
canonical EQUS.MINI 1h stock bars + canonical OPRA.PILLAR 1h option surfaces
                                |
                   pooled causal sequence encoder
                                |
               calibrated distribution + representation
                    /                    |                 \
       Loop B shadow report   Options Strategies report   Loop C observe
                                                             |
                                      deterministic risk/reconciliation/halt gates
                                                             |
                                              research proposal or no trade
                                              (orders_enabled=false)
```

Loop B remains authoritative at `ml/latest/run.json`; Options Strategies
remains authoritative at `ml/strategy-latest/run.json`. The shared encoder has
its own `ml/sequence-encoder-latest/run.json`, and Loop C observe results have
their own `ml/loop-c-latest/run.json`. Cross-lane files are consumed only after
manifest, receipt, checksum, timestamp, and authority verification.

## Causal model calculus

For symbol `s` and completed hourly state `t`, the fixed state vector contains
stock OHLCV behavior plus an OPRA surface summary across call/put, moneyness,
DTE, price, range, volume, contract count, and explicit missingness. Duplicate
publisher rows for the same contract/hour are consolidated before aggregation.
Every state carries its evidence-availability timestamp.

Given the prior `W=32` states, the pooled LSTM produces one representation
`h(s,t)`. Causal pretraining predicts the next normalized state using only
states available through `t`. Supervised fine-tuning uses the existing exact
Loop B 1h, 4h, 1d, and 1w target definitions:

```text
loss = decision_weight * (binary_cross_entropy(direction)
                          + Gaussian_NLL(cost_adjusted_return))

decision_weight = 1 / number_of_symbols_sharing_the_target_cluster
```

This prevents a large surface or many symbols sharing one market decision from
masquerading as independent evidence. Training, probability calibration, and
assessment are chronological and boundary-purged. Assessment never selects or
fits the model.

An ensemble separates uncertainty as:

```text
aleatoric variance = mean(member conditional return variance)
epistemic variance = variance(member conditional return means)
total variance      = aleatoric variance + epistemic variance
```

Direction probabilities receive horizon-specific Platt calibration. Return
intervals receive a horizon-specific scale fitted only on the calibration
partition. Publications expose calibrated probability-up, expected return,
10/50/90 return quantiles, aleatoric uncertainty, epistemic uncertainty, total
uncertainty, and a 32-value representation. None grants action authority.

## Loop C deterministic calculus

Options Strategies supplies the calibrated probability that an exact strategy
has positive net return after modeled execution and fees. The sequence encoder
separately supplies directional probability, expected underlying return,
adverse quantile, and uncertainty. Loop C does not relabel either meaning.

Directional structures must pass the sequence probability in the direction of
their net delta; delta-neutral structures receive no directional veto. Every
candidate must then pass explicit, versioned deterministic limits for:

- strategy calibrated probability and sequence directional support;
- expected return on risk and uncertainty-penalized utility;
- modeled loss and capital per unit;
- projected available cash, gross exposure, and per-symbol exposure;
- open-position and working-order counts;
- portfolio and broker snapshot freshness and reconciliation;
- model-publication freshness, market-session status, daily loss, unknown
  broker submission state, and the independent halt control.

Position size is the minimum integer capacity allowed by maximum trade loss,
available cash, remaining gross exposure, remaining symbol exposure, and the
explicit quantity cap. The model cannot modify any limit. Missing inputs block
the candidate or the entire decision. A daily-loss breach or halt request
produces `HALT`. Version 1 still emits only `RESEARCH_PROPOSAL`, `NO_TRADE`,
review, or halt records and always records zero orders.

Historical replay is ordered by event availability time, event time, source
sequence, and type. Replay wall-clock speed is deliberately absent, so a fast
replay cannot make evidence arrive earlier than it did historically.

## Scheduler behavior

Ordinary open-market wakes may run only bounded inference against an already
verified model and only when the current Loop B source changed. Training is
reserved for a stage-13-preregistered `run-shadow-ablation`; it is never an
ordinary hourly job. Loop C is skipped unless operator-approved risk limits and
fresh reconciled portfolio, broker, and halt snapshots all exist.

The durable source of truth is
[`HOURLY_AUTOMATION.md`](HOURLY_AUTOMATION.md). The scheduler must preserve its
current checksum-valid handoff stage: adding this lane does not permit a new
experiment to displace an in-progress compare/stress/freeze sequence.

## Commands

Train a preregistered bounded challenger without publishing a shadow pointer:

```powershell
.\.venv\Scripts\python.exe -m ml.sequence_encoder.runtime `
  --datastore-target pc `
  --information-cutoff <preregistered-causal-cutoff> `
  --maximum-sessions-per-symbol <preregistered-bound> `
  --compact
```

Add `--publish-shadow` only after the complete immutable run verifies. Current
open-market inference is idempotent by source Loop B generation:

```powershell
.\.venv\Scripts\python.exe -m ml.sequence_encoder.inference_runtime `
  --datastore-target pc `
  --information-cutoff <current-loop-b-causal-cutoff> `
  --run-timestamp <current-utc> `
  --require-market-open `
  --compact
```

Loop C requires explicit JSON inputs and remains observe-only:

```powershell
.\.venv\Scripts\python.exe -m ml.loop_c.runtime `
  --datastore-target pc `
  --decision-timestamp <current-utc> `
  --risk-limits <approved-risk-limits.json> `
  --portfolio-snapshot <fresh-reconciled-portfolio.json> `
  --broker-snapshot <fresh-reconciled-broker.json> `
  --halt-control <current-halt-control.json> `
  --compact
```

No risk-limit values are checked in or silently defaulted: they are an explicit
operator decision and a prerequisite to running even the observe lane.

## Activation gates

Any future request to let this lane affect rankings or orders is a separate
production change. At minimum it requires prospective shadow evidence across
multiple regimes, calibration and interval coverage by horizon, comparison to
the existing base-rate and production models, cost/slippage stress, missing
data and latency stress, stable symbol/cohort behavior, reproducible immutable
receipts, explicit risk-limit approval, broker paper-trading reconciliation,
kill-switch drills, rollback tests, and direct user approval. No scheduler
stage can infer that approval from favorable metrics.
