# Pooled causal sequence encoder and Loop C

## Authority boundary

This is a production-hardened **shadow and observe-only** lane. It is designed
to accumulate prospective evidence safely before any separate activation
proposal. It does not replace Directional Loop B, alter Options Strategies
ranking, or expose a broker order method. Its Schwab lane is limited to the
existing Duckets read-only account, order-history, and transaction-history
methods and persists only identifier-free evidence.

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

The thresholds are split by `1h`, `4h`, `1d`, and `1w`; a candidate cannot
borrow a looser probability or uncertainty gate from another horizon. Position
size is the minimum integer capacity allowed by maximum trade loss,
available cash, remaining gross exposure, remaining symbol exposure, and the
explicit quantity cap. The model cannot modify any limit. Missing inputs block
the candidate or the entire decision. A daily-loss breach or halt request
produces `HALT`. Version 2 still emits only `RESEARCH_PROPOSAL`, `NO_TRADE`,
review, or halt records and always records zero orders.

Historical replay is ordered by event availability time, event time, source
sequence, and type. Replay wall-clock speed is deliberately absent, so a fast
replay cannot make evidence arrive earlier than it did historically.

Every successful `RESEARCH_PROPOSAL` is an exact Options Strategy paper entry,
not a generic directional option bet. The immutable report freezes its Strategy
run, candidate ID and key, complete stock/option legs, entry BBO assumptions,
fees, paper quantity, capital, modeled loss, model evidence, target window, and
per-leg signed BUY/SELL plus gross exercise/assignment share obligations.
Stateful constructions without
a bounded receipt-proven target exit are ineligible. The paper lane creates no
broker payload and cannot cause exercise or assignment.

## Scheduler behavior

Ordinary open-market wakes may run only bounded inference against an already
verified model and only when the current Loop B source changed. Training is
reserved for a stage-13-preregistered `run-shadow-ablation`; it is never an
ordinary hourly job. Loop C is skipped unless an operator-approved exact model
binding and risk limits plus fresh reconciled portfolio, broker, and halt
snapshots all exist.

This is an authority boundary, not a waiting period for useful computation.
Loop B and Options Strategies begin consuming verified sequence distributions
through their checked-in report seams as soon as the first stage-14 model run
publishes. Loop C begins emitting observe-only decisions on the first open
session wake for which all four inputs validate. The prospective evidence clock
starts with that first successful Loop C observation; it limits only a later
authority-expansion proposal.

The durable source of truth is
[`HOURLY_AUTOMATION.md`](HOURLY_AUTOMATION.md). The scheduler must preserve its
current checksum-valid handoff stage: adding this lane does not permit a new
experiment to displace an in-progress compare/stress/freeze sequence.

## Commands

At a scheduler-selected stage 13, construct the receipt actions from the
current verified source without mutation:

```powershell
.\.venv\Scripts\python.exe -m ml.sequence_encoder.preregistration_proposal `
  --datastore-target pc `
  --eligible-session <guardian-eligible-session> `
  --symbol <each-preregistered-symbol> `
  --maximum-sessions-per-symbol <preregistered-bound> `
  --compact
```

The scheduler copies the returned `handoff_actions` verbatim into its stage-13
receipt. Merely running this read-only builder is not a preregistration and does
not select the experiment.

Train a preregistered bounded challenger without publishing a shadow pointer:

```powershell
.\.venv\Scripts\python.exe -m ml.sequence_encoder.runtime `
  --datastore-target pc `
  --preregistration-receipt <current-stage-13-handoff-receipt.json> `
  --compact
```

The receipt is the source of the exact cutoff, symbols, data bound, frozen Loop
B checksums, configuration fingerprint, metrics, and stop conditions. Optional
CLI duplicates of the cutoff, symbols, or bound must match it exactly. A stale,
non-current, non-stage-13, checksum-invalid, or source-mismatched receipt fails
before training. The accepted fingerprint is atomically consumed once before
fitting; a failure or interruption is terminal evidence and cannot silently
retry the same experiment. Add `--publish-shadow` only for the same bounded
stage-14 run; publication still grants only `SHADOW_ONLY` authority. Current
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

Before validation, refresh the existing Schwab Duckets read-only evidence:

```powershell
.\.venv\Scripts\python.exe -m ml.loop_c.schwab_snapshot `
  --datastore-target pc `
  --compact
```

This command may use authenticated GET-only account, working-order, bounded
order-history, and transaction-history calls. It cannot call submit, replace,
or cancel. It writes receipt-backed, identifier-free portfolio and broker
snapshots. Build an auditable proposal from those snapshots without approving
it:

```powershell
.\.venv\Scripts\python.exe -m ml.loop_c.risk_proposal `
  --datastore-target pc `
  --compact
```

The proposal binds the exact planned sequence configuration fingerprint,
calculates equity-relative caps, applies only a downward history throttle, and
writes `PENDING_OPERATOR_APPROVAL`. It never writes the canonical approved risk
file or an unhalt control. The full math is documented in
[`LOOP_C_RISK_CALCULUS.md`](LOOP_C_RISK_CALCULUS.md).

After the operator reviews one exact pending artifact, the receipt-backed
`ml.loop_c.operator_controls` issuer may apply only that immutable proposal as
a weekly `LOOP_C_OBSERVE_ONLY` lease. It requires the identity, rationale,
Friday 17:00 Pacific expiry, and an explicit halt/unhalt choice; archives the
issuance lineage; and still has no broker-order capability. The exact command
is documented in [`LOOP_C_ROLLOUT_PLAN.md`](LOOP_C_ROLLOUT_PLAN.md).

The separate Saturday review captures the week's read-only account context,
joins Loop C proposals only to causally mature receipt-matched outcomes, and
builds the next pending proposal:

```powershell
.\.venv\Scripts\python.exe -m ml.loop_c.weekly_review `
  --datastore-target pc `
  --capture-schwab `
  --build-risk-proposal `
  --compact
```

The independent daily paper ledger materializes the lifecycle of those exact
selections without contacting Schwab:

```powershell
.\.venv\Scripts\python.exe -m ml.loop_c.paper_ledger `
  --datastore-target pc `
  --compact
```

Its summary keeps open targets separate from mature and evidence-pending rows
and exposes current BUY-share, SELL-share, total gross, maximum single-position,
and earliest-expiration obligations for Saturday review.

It is scheduled at 00:17 Pacific Tuesday through Saturday, after the prior
market day's causally mature Strategy-outcome evaluation. It distinguishes
`OPEN_PENDING_TARGET`, `PENDING_MATURE_OUTCOME_EVIDENCE`, and
`MATURE_RECEIPT_MATCHED`; an interim mark is never used as a final result. The
durable task contract is
[`OPTIONS_STRATEGY_PAPER_AUTOMATION.md`](OPTIONS_STRATEGY_PAPER_AUTOMATION.md).

Its durable authority and attribution rules are in
[`WEEKLY_REVIEW_AUTOMATION.md`](WEEKLY_REVIEW_AUTOMATION.md). It cannot approve
the proposal, unhalt Loop C, retrain or swap the bound model, or call a broker
mutation.

No risk-limit proposal is self-approved: every value remains an explicit
operator decision and a prerequisite to running even the observe lane. The
scheduler looks only at these canonical current-input locations:

```text
C:\DATASTORE\controls\loop-c\current\risk-approval.json
C:\DATASTORE\controls\loop-c\current\portfolio-snapshot.json
C:\DATASTORE\controls\loop-c\current\broker-snapshot.json
C:\DATASTORE\controls\loop-c\current\halt-control.json
```

The four non-authoritative examples in [`templates`](templates) contain no
approved numeric values. The version-2 risk file requires an unexpired
`APPROVED` record scoped to `LOOP_C_OBSERVE_ONLY`, an exact pooled-sequence model
binding, and all four horizon threshold records. Portfolio and broker files
require fresh `OBSERVED_READ_ONLY`, reconciled state plus a checksum-valid
source receipt under the datastore; their schemas intentionally exclude
account identifiers. The halt control has an independent issue time and expiry
and cannot be inferred from the risk approval. Validate all four without
running Loop C by adding `--validate-inputs-only` to the command above.

## Activation gates

Any future request to let this lane affect rankings or orders is a separate
production change. `loop-c-options-1d-plus-observe-evidence-gate-v2` permits an
operator review no sooner than 40 completed XNYS sessions (about eight trading
weeks) and also requires at least 30 mature independent 1d option-paper
clusters, eight non-overlapping weekly option-paper cohorts, 20 reconciled
observations, two halt drills, one rollback drill, passing calibration and
interval coverage, cost/latency/missing-data stress, symbol/regime stability,
publication integrity, paper-broker reconciliation, zero deterministic-gate
violations, and zero orders. These are conjunctive floors, not a countdown to
automatic activation. `ml.loop_c.rollout` can return only
`ELIGIBLE_FOR_OPERATOR_REVIEW`; it always withholds authority and automatic
promotion. A separate production change and direct user approval remain
mandatory, regardless of favorable metrics or scheduler stage.

The shared encoder still publishes `1h`, `4h`, `1d`, and `1w` distributions for
Loop B and other shadow consumers. Loop C's options paper lane selects only
`1d` and `1w` Strategy candidates; shorter option horizons are logged as
`OPTIONS_SHADOW_HORIZON_BELOW_1D` and cannot become a research proposal.
Here `1w` means the canonical dynamic **Remaining-Week Aggregate**. It spans a
full five sessions only when issued before that complete exchange week; a
midweek `1w` entry covers the remaining eligible sessions and must not be
reported as a fixed five-session contract.
