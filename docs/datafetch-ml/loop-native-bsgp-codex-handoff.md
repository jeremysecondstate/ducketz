# Loop-native causal BSGP Codex handoff

You are continuing audited option-pricing production-readiness work in:

- Workspace: `C:\dev\ducketz`
- Datastore: `C:\DATASTORE`
- Time baseline when this handoff was written: August 10, 2026 Pacific / August 11 UTC
- Research source: `docs/edu/BLACK-SCHOLES-OP.md`

The operator has settled the architectural direction described below. Implement it faithfully,
but do not weaken any audit, causality, prospective-evidence, spend, or automation guard.

## Settled decision

Replace mandatory paid OPRA history with a Loop-native causal BSGP research lane trained from
immutable Schwab option snapshots and matured Loop outcomes across the complete ten-symbol
watchlist:

`NVDA GOOG MU AAPL MSFT AMZN META TSLA CAT SNDK`

Black-Scholes remains the active, fail-closed baseline. BSGP is published side by side in shadow
and learns only the market discrepancy:

```text
predicted American option value = causal Black-Scholes value + GP residual correction
```

This follows the central construction in the research, `f(x) = BS(x) + delta(x)`, while adapting
the observed-data source and chronology to the Loop system. Paid OPRA is optional external
benchmark data only. It is not a prerequisite for the new policy and must not be purchased in
this task.

The settled direction does **not** authorize automated trading, candidate promotion, model
freeze, lockbox access, rankings changes, order construction, paid data, or retrospective claims
of prospective evidence.

## Start here

Before changing anything:

1. Read this file and `docs/edu/BLACK-SCHOLES-OP.md` completely.
2. Inspect `git status`, every existing uncommitted diff, and all untracked files.
3. Inspect the current live process arguments and datastore artifacts without stopping anything.
4. Review the existing Pricing, Options, model, eligibility, publication, lineage, and Strategy
   tests and implementation.
5. Rerun the relevant tests before editing.

The worktree was already intentionally dirty when this handoff was created. It included an
immutable FRED/ALFRED vintage importer, an inactive GOOG no-trade-minute proposal, tests, and
documentation, plus operator-owned changes that deleted the old BSGP prompt and copied the
research into `docs/edu/BLACK-SCHOLES-OP.md`. Preserve all of it. Do not restore deleted files,
discard changes, overwrite unrelated work, stage, or commit unless explicitly instructed.

Do not terminate or restart live processes without explicit operator approval. In particular,
leave CME and directional Loop B alone. Do not call Databento `timeseries.get_range`; no OPRA
spend is authorized. Never print provider credentials.

## Current audited state to re-verify

Treat these as a starting point, not timeless truth:

- Gate 1 passes; Gates 2-10 are `NOT_PROVEN`.
- `automated_action_allowed=false` is mandatory everywhere.
- Operational preflight is PASS.
- Live Black-Scholes surface-rate readiness is PASS for all ten symbols.
- The current FRED receipt is causal only for later targets. It makes no historical claim.
- Historical point-in-time FRED/ALFRED coverage has not yet been acquired.
- The relevant suite passed 123 tests after the prior work.
- Prospective pilot routes had only one distinct qualifying session and still required 19 more.
- The earliest possible prior 20-session threshold date was September 4, 2026.
- No candidate is frozen and no lockbox is open.
- No OPRA definitions or CBBO data have been downloaded.
- A metadata-only estimate for definitions across all ten symbols was `$10.537075`, but the
  operator explicitly chose the no-paid-OPRA direction. That estimate is not authorization.

At the last read-only inventory, committed Schwab history contained approximately:

- 460-461 committed publications per symbol;
- 45-47 distinct regular-session `snapshot_for` targets per symbol;
- four distinct sessions for most symbols and five for CAT; and
- 11,705,355 raw contract rows before natural-target deduplication.

There were hundreds of duplicate publications and as many as 229 publications for one natural
target. Recompute these figures. Never interpret publications or contract rows as independent
sessions.

## Existing architectural defect to correct

The current runtime already materializes OPRA history and fits/reuses finite-basis residual
models after its fast target publication. However, the live fast path currently calls
`create_prediction_rows(..., models={})`. Consequently, live publications are Black-Scholes
baseline-only even when a residual model exists.

Correct this without delaying the causal target publication:

- train or update a shadow model only after the fast publication;
- publish an immutable model generation for a future cycle;
- at the next cycle, verify and load the earlier model before fast publication; and
- pass only that earlier verified model into shadow inference.

A model trained on or published after the natural target cutoff must never affect that target.

## Required Loop-native data flow

Implement this causal sequence:

1. Loop A commits the exact completed underlying bar and readiness proof.
2. Pricing selects an earlier committed Schwab chain and a model artifact published strictly
   before the prediction cutoff.
3. Pricing publishes the Black-Scholes baseline and optional BSGP shadow result before Options.
4. Loop 4 captures and commits the later exact target Schwab quote.
5. A later Pricing cycle reconciles the prediction with that target receipt.
6. Only a matured outcome whose receipt predates the trainer cutoff may enter model training.
7. The resulting immutable model generation becomes eligible only for a later target.

Training must make no external provider request. It reuses already committed local artifacts.

## Schwab history materialization contract

Build a new versioned, receipt-proven materializer for Loop-native BSGP evidence. Do not relabel
the OPRA materializer or silently reinterpret old evidence.

### Natural target and duplicate policy

- The natural snapshot key is `(symbol, snapshot_for)`.
- Select the earliest valid committed receipt that satisfies the applicable cutoff.
- Later publications for the same natural target are lineage/diagnostic observations only and
  must not multiply training weight.
- Record every consulted receipt and the deterministic selection reason.
- Fail closed on conflicting semantic contract data, checksum failures, path escapes, or
  unverifiable timestamps.

### Offline bootstrap lane

Existing Schwab snapshots may be replayed into causal source/target pairs only when their actual
receipt and quote clocks prove the reconstruction. Label them explicitly, for example
`OFFLINE_SCHWAB_BOOTSTRAP`. They may train and support offline assessment but must never increment
prospective prediction or distinct-session counts.

For a bootstrap prediction:

- the source snapshot and every input must be available before the emulated prediction time;
- the target snapshot must be later and must never supply an input feature;
- source and target contracts must match semantically by symbol, expiration, call/put, strike,
  multiplier, and standard-contract status;
- the target label is a finite uncrossed bid/ask midpoint with an exact quote clock; and
- missing source, target, underlying, rate/carry, or contract identity remains missing.

Do not manufacture backdated prediction receipts. Bootstrap evidence is offline by construction.

### Prospective lane

A prospective outcome qualifies only when the immutable Pricing publication precedes the exact
Loop 4 target receipt and the existing target-authority/barrier proofs pass. Preserve the existing
20-distinct-session requirement. Never simulate, backfill, backdate, or weaken it.

### Contract and liquidity filters

Retain explicit versioned filters for standard non-mini 100-share CALL and PUT contracts,
7-120 calendar days to expiration, `abs(log(K/S)) <= 0.25`, finite positive uncrossed BBO,
source/target quote clocks, spread, staleness, and contract continuity. Preserve rejection
reasons instead of silently dropping rows.

## Causal rate, carry, and volatility inputs

Never use current-revised history as if it had been known earlier.

For future targets, prefer a verified FRED/rate receipt whose `available_at` is no later than the
prediction cutoff. For bootstrap rows predating causal provider-rate coverage, either exclude the
row or use a separately versioned source-chain implied-forward/discount policy that passes all
quality checks.

If implementing the source-chain fallback:

- derive it only from the earlier committed source snapshot;
- use robust matched call/put cohorts across strikes and the same expiration;
- record pair count, fit residuals, spread/staleness limits, bounds, and uncertainty;
- account explicitly for the fact that American put-call parity is an inequality/approximation;
- never present the result as an observed risk-free rate or FRED history;
- reject unstable or weakly identified fits; and
- allow the Black-Scholes implementation to consume a verified forward/discount pair directly
  if that is more defensible than separating `r` and `q`.

Implied volatility must come from the earlier source chain only. Never solve or copy target-time
implied volatility into the prediction features. Target-time IV may appear only in later
evaluation diagnostics.

## BSGP model contract

Stay close to the research while retaining the repository's scalable implementation.

### Mean and target

- Black-Scholes is the mean/prior.
- Learn the normalized market residual, preferably
  `(target_midpoint - causal_black_scholes_price) / target_underlying_price`.
- Record the unnormalized dollar residual for interpretation.
- Keep a pure Black-Scholes row/value for every valid prediction.

### Features

Use the six research concepts with causal transformations:

- underlying price;
- strike or log-moneyness;
- verified rate/discount input;
- lagged source implied volatility;
- time to expiration; and
- verified dividend/carry or forward input.

Do not add opaque features merely to improve in-sample fit. Additional regime or liquidity
features require explicit policy versions, ablations, and evidence.

### Pooling and route adaptation

The production watchlist is all ten symbols. Start with two scale-aware pooled finite-basis GP
models, one for CALL and one for PUT, so the small number of independent sessions is not hidden
behind millions of correlated contracts. Add route-specific intercept/scale shrinkage only when
that symbol/side has sufficient independent sessions.

Every `(symbol, target_snapshot_for, call_put)` surface must have bounded total training weight.
No large chain or duplicated snapshot may dominate the objective.

### Approximation and uncertainty

Reuse or carefully extend the existing Nyström RBF plus Bayesian-ridge finite-basis GP. Do not
introduce an unbounded exact Gaussian process. Maintain deterministic seeds, bounded components,
row/runtime/memory limits, and immutable artifacts.

Calibrate predictive intervals on chronologically later, session-blocked data. Report GP
posterior uncertainty separately from bid/ask spread, input staleness, and out-of-support risk.
When calibration is immature, say so and use conservative fallback status rather than claiming
calibrated uncertainty.

### Shrinkage and fallback

The residual correction must shrink toward zero when support is sparse, the model is stale,
uncertainty is excessive, or the input is out of support. No valid model means Black-Scholes
baseline, not a failed publication.

Use explicit statuses such as:

- `BSGP_SHADOW_READY`
- `BASELINE_FALLBACK_NO_MODEL`
- `BASELINE_FALLBACK_STALE_MODEL`
- `BASELINE_FALLBACK_OUT_OF_SUPPORT`
- `BASELINE_FALLBACK_INPUT_UNAVAILABLE`
- `BASELINE_FALLBACK_UNCALIBRATED`

Retain positivity, American lower/upper bounds, strike monotonicity, convexity, and interval
nesting projections. Record raw and constrained values and every projection magnitude.

An American-option approximation such as CRR, Barone-Adesi-Whaley, or Bjerksund-Stensland may be
added as a diagnostic comparator only in this version. Do not silently replace the settled
Black-Scholes mean with it.

## Immutable model lifecycle

Create a new versioned model-generation contract rather than reusing OPRA identities.

Each model generation must record at least:

- schema and policy versions;
- pooled CALL/PUT scope and all ten configured symbols;
- exact training, calibration, and assessment session boundaries;
- `trained_through`, `published_at`, `effective_from`, and expiry/maximum-age policy;
- every selected input receipt and materialized-sample checksum;
- duplicate-collapse and surface-weight reports;
- feature transforms, basis seed/components/gamma, regression parameters, and library versions;
- support bounds and route-level support statistics;
- interval-calibration evidence;
- offline/bootstrap versus prospective row/session counts;
- comparator metrics; and
- `automated_action_allowed=false`.

Publish atomically through a checksummed pointer. A model is usable only if the entire reachable
chain verifies and `published_at < prediction_created_at`. A model may not update in place.

Training runs after the fast target publication or in another nonblocking bounded stage. It may
include only outcomes available strictly before its cutoff and may affect only later cycles.

## Live publication and consumer isolation

Move verified model loading ahead of live shadow inference, but do not put fitting in the fast
target path.

Publish Black-Scholes and BSGP shadow values side by side with explicit lineage. Prefer new
versioned shadow columns/artifacts over changing existing natural keys. Include at least:

- `black_scholes_price`
- `bsgp_shadow_fair_value_raw`
- `bsgp_shadow_fair_value_constrained`
- `bsgp_shadow_normalized_residual`
- `bsgp_shadow_predictive_standard_deviation`
- raw and constrained 80/95 percent intervals
- `bsgp_shadow_status`
- model generation path/hash and `trained_through`
- support/shrinkage diagnostics
- `automated_action_allowed=false`

Until a later explicit promotion is approved:

- existing baseline `constrained_fair_value` behavior must not change;
- Strategy rankings, candidate selection, order construction, and payloads must not change;
- directional Loop B's default profile must not read the shadow values;
- `--pricing-mode shadow` may observe and evaluate them only; and
- missing or invalid BSGP must degrade to baseline without delaying Pricing or Loop 4.

## Eligibility and policy migration

Do not edit the existing audited eligibility policy in place. Publish a new version and hash for
the Loop-native ten-symbol research scope. Preserve the old three-symbol/OPRA policy and every
historical report as readable legacy evidence.

The new policy must make paid OPRA optional and replace its mandatory historical-input gate with
receipt-proven Schwab causal-residual evidence. It must require all twenty symbol/side routes.

At minimum, the new gates must continue to require:

- complete immutable lineage and timing;
- real, non-fixture Schwab source and target receipts;
- causal input availability and no same-target leakage;
- session-blocked training/calibration/assessment partitions;
- BSGP improvement over Black-Scholes, constant-residual, and standard-GP comparators;
- calibrated uncertainty and constraint compliance;
- sufficient liquidity/coverage and exact route retention;
- operational latency/capacity proof;
- Strategy shadow outcome evidence including fees and spread where required;
- at least 20 distinct prospective sessions per required route; and
- explicit candidate/lockbox/operator authorization stages after all prerequisites pass.

The existing Gates 2-10 remain `NOT_PROVEN` until real evidence satisfies the new policy. A policy
migration is not evidence. Do not freeze a candidate or open the lockbox while implementing it.

## Research caveats to encode, not hide

The thesis used SPY data from May-June 2019 and showed a one-week-ahead example. Its reported MSE
must not be assumed transferable to ten equities in 2026. Its use of implied volatility must not
be copied in a way that derives a predictor from the price being predicted. Its many option rows
are cross-sectionally and temporally dependent.

The implementation must improve on those limitations through causal lagged volatility,
session-blocked assessment, equal surface weighting, explicit uncertainty calibration, and
prospective Loop evaluation. Document deviations from the paper precisely.

## Required tests

Add focused tests before or with the implementation. Include at least:

1. Duplicate Schwab publications collapse to the earliest valid receipt per natural target.
2. Conflicting duplicate receipts fail closed.
3. Offline bootstrap reconstruction never increments prospective counts.
4. The target snapshot, target IV, and later receipts cannot enter prediction features.
5. Same-cycle matured outcomes cannot train the model used for that cycle.
6. A model published at or after prediction creation is rejected.
7. Model expiry, out-of-support, missing input, and failed verification all fall back to BS.
8. A valid earlier model produces separate BSGP shadow values before the Options receipt.
9. Black-Scholes baseline values remain unchanged with and without a shadow model.
10. All ten symbols and both CALL/PUT routes are retained in scope and reports.
11. Surface/session weighting is invariant to duplicate contracts/publications.
12. Chronological session partitions and lockbox redaction cannot leak.
13. Source-chain carry inference, if implemented, is causal, quality-gated, and never labeled FRED.
14. Current-revised rate history is rejected for historical bootstrap coverage.
15. Shadow values cannot alter Strategy rankings, order construction, or order payloads.
16. No OPRA `get_range` call can occur from Loop A, Pricing, Options, Strategy, materialization,
    model training, evaluation, or readiness.
17. Runtime limits and fast-publication ordering still pass.
18. Every new artifact and pointer fails closed on path escape, tamper, schema drift, or checksum
    mismatch.

Update affected existing tests rather than weakening their assertions. Retain tests that prove
the old OPRA path is guarded and optional.

## Verification workflow

After implementation:

1. Run the focused preexisting option-pricing suite and all new tests.
2. Run `git diff --check`, compile/help smoke tests, and operational preflight.
3. Run an actual-datastore read-only/dry-run materialization over the ten symbols.
4. Report selected versus duplicate snapshots, contract/surface rows, independent sessions,
   exclusion reasons, source clocks, rate/carry coverage, runtime, and memory.
5. Exercise a simulated cycle proving model generation `n-1` can affect only shadow output for
   target `n` while baseline values remain byte/semantically stable.
6. If a real regular-session sequence occurs, monitor the exact Loop A -> Pricing -> Options
   chronology and publish evidence. Do not manufacture it if the session has passed.
7. Rerun readiness and Strategy evaluation. Expect research gates to remain `NOT_PROVEN` until
   sufficient real outcomes mature.

Do not restart the live runtimes yourself without explicit approval. If a restart is needed,
report the exact PIDs/arguments and ask permission to replace only the named processes.

## Acceptance criteria

The implementation is complete only when all of the following are true:

- Paid OPRA is no longer a prerequisite of the new policy and no paid data was downloaded.
- Existing OPRA code remains guarded and optional or is isolated through a compatible legacy
  reader; old evidence is never reinterpreted.
- Receipt-proven Schwab source/target pairs train the residual lane causally.
- The live Pricing fast path can load a strictly earlier verified shadow model.
- BS and BSGP are published side by side, with BS retaining control behavior.
- The model updates from matured Loop outcomes without an external data call.
- Duplicate publications and correlated contracts cannot inflate sessions or training weight.
- All ten symbols and twenty CALL/PUT routes remain visible, including missing routes.
- Current-revised historical rates are never substituted.
- Strategy and orders are unchanged.
- `automated_action_allowed=false` remains enforced.
- No candidate is frozen and no lockbox is opened.
- Tests and actual-datastore dry-run evidence are reported with exact counts, paths, hashes,
  timings, and blockers.

## Final report vocabulary

Keep these conclusions separate:

- **Capture-ready**: the Loop-native shadow system is operational, causal, fail-closed, and
  collecting its own evidence.
- **Research-gate eligible**: sufficient independent session-blocked evidence exists to evaluate
  the model under the new policy.
- **Production authorized**: every gate, candidate, lockbox, operational, Strategy, and explicit
  operator authorization requirement has passed.

Implementation success may establish capture-readiness. It does not by itself establish
research-gate eligibility or production authorization.
