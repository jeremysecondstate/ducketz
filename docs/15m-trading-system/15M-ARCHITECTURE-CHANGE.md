# Codex: Implement 15-Minute Duckets — Continuously Retrained Multi-Model Ensemble

I want to extend my existing Schwab/Ducketz trading system with a continuously retrained, heterogeneous ensemble forecasting engine and a dedicated **15-Minute Duckets** UI tab.

Read `INTRADAY_15M_SYSTEM_DESIGN.md` and inspect the actual repository before modifying anything. Preserve the existing architecture and its settled decisions. Do not build a separate replacement system.

The [consolidated design](INTRADAY_15M_SYSTEM_DESIGN.md) includes the later
October 12 decisions: per-symbol publication, inclusion of confirmed synthetic
no-trade outcomes with separate breakdowns, all compatible history for the first
timed benchmark, per-symbol technical-indicator sizing every 15 minutes,
the approved independent `0.015 * account equity / fresh side price` scale and
half-up nearest-share rounding, actual shared capacity without stream budgets,
replacement of prior unfilled intentions only after cancellation/status is
confirmed, and a strict 16:45 Pacific submission cutoff. These clarifications
govern the proposal below. Development now also targets a human-started Atlas
live launcher, followed by Scout after Atlas observation. Source now implements
the Atlas manual session and shared-account execution adapter with offline
injected-broker verification. Publication, local installation, compatible
loaded Gameplan and a human-started live run remain separate evidence; no live
launcher was started or production worker restarted by this change.

## 1. Objective

Both Atlas and Scout should run the same configurable forecasting architecture on separate PCs, using their respective 11-symbol universes and local DATASTOREs.

For each symbol, every 15 minutes, after receiving a completed candle:

1. Ingest and validate the completed OHLCV candle.
2. Update causally available features and technical indicators.
3. Finalize any earlier prediction targets that are now observable.
4. **Fully retrain the enabled ensemble models** on the configured historical window.
5. Generate individual model probabilities for the next defined target.
6. Calculate an equal-weight ensemble consensus.
7. Persist individual predictions, ensemble prediction, versions, timestamps, and target information.
8. Score the prediction automatically once its target observation becomes available.
9. Update the Ducketz dashboard with fresh predictions, outcomes, and performance metrics.

Full retraining every 15 minutes must be the default research implementation. Do not silently replace it with daily training, incremental-only updates, or occasional retraining. Computational feasibility should be measured, not assumed.

## 2. Preserve the Exact Prediction Target

The attached design's target is **not-down**.

For example, after the 10:00–10:15 candle completes, compare the 10:30–10:45 candle's CLOSE against the reference candle's OPEN at 10:00.

The target is:

`P(target_close >= reference_open)`

Equality counts as not-down. This is approximately a 30-minute remaining forecast over a 45-minute reference-open-to-target-close period.

Store reference candle time/open, target candle time/close, prediction issue time, data-through time, actual data availability, and target-definition version.

Keep this target separate from older strictly-positive Gameplan labels. Never use an incomplete or future candle to train or score a prediction. Previously issued forecasts remain immutable.

## 3. Build a Configurable Heterogeneous Ensemble

Implement a reusable model registry with a configurable enabled subset, initially approximately 10 independently trained classifiers, drawn from the following model types. Include the types represented in my prior model code alongside the originally proposed options:

- XGBoost
- LightGBM
- CatBoost
- Random Forest
- Extra Trees
- Histogram Gradient Boosting
- Gradient Boosting
- AdaBoost
- Logistic Regression
- k-Nearest Neighbors (KNN)
- Linear Discriminant Analysis (LDA)
- Quadratic Discriminant Analysis (QDA)
- Gaussian Naive Bayes
- Passive Aggressive classifier
- Ridge classifier
- Multilayer Perceptron
- 1D Convolutional Neural Network
- LSTM with attention
- Bidirectional GRU with attention
- Stacked bidirectional LSTM with attention (the prior `rnn` implementation)
- ATTN attention network
- AFFN attention/feed-forward network
- CSANN attention/feed-forward network
- Dilated convolution-LSTM (DC-LSTM)
- GARN bidirectional GRU with attention and gating
- Temporal Convolutional Network (TCN)
- TFT-style gated residual attention model
- Time-series Transformer encoder, including the prior Transformer A/B variants

The prior model directory contains implementation code and prediction adapters, not fitted model artifacts. KNN, LDA, QDA, Gaussian Naive Bayes, Passive Aggressive, and Ridge have prediction adapters only in that directory; verify or implement their training and probability interfaces before enabling them. Histogram Gradient Boosting and Multilayer Perceptron remain proposed options from the original list.

Treat named neural variants as separate registry entries where their architectures differ. Retrain every enabled model for this document's exact not-down target; prior model code does not establish compatibility with that target or readiness for every-15-minute retraining.

Inspect available dependencies before selecting implementations. Do not substitute fake models for unavailable architectures. Report unsupported models and installation requirements.

Tree models may use tabular causal technical features. Neural networks may use historical sequences. All architectures must produce probabilities for the identical not-down target.

Train models independently, record their versions and training cutoffs, and preserve deterministic configurations where possible.

Support configurable and as deep as time will allow us (we must keep timing TIGHT) historical lookbacks, this could be anywhere between 1 and five years where usable history exists. And allow shorter rolling-window experiments.

## 4. Implement Equal-Weight Consensus

Each model produces a not-down probability.

The default ensemble probability is:

`ensemble_probability = mean(individual_model_probabilities)`

All included model probabilities receive equal weight.

A probability at or above 0.50 maps to the baseline not-down classification; below 0.50 maps to down. This classification threshold is not a new broker execution gate.

Log every model's output and the exact participating model count. If some models fail, distinguish an incomplete/degraded ensemble from the fully configured consensus; do not invent an unapproved execution fallback.

Optionally add calibrated-probability averaging, performance weighting, and stacking as separate research alternatives, but preserve the simple arithmetic mean as the primary benchmark.

## 5. Every-15-Minute Training and Prediction Pipeline

Build reliable scheduling, training, inference, and storage around each new validated 15-minute candle.

Use the actual historical data already available in the DATASTORE. Anchor
acquisition to each quarter-hour boundary plus a configurable provider
publication offset. A 30-second candidate means 10:00:30, 10:15:30, 10:30:30 and
10:45:30, not an interval of 15 minutes 30 seconds. Measure the useful offset;
elapsed time alone does not establish that the expected completed data is
available. Validate the exact interval and retain persistent retry evidence.
Update price history incrementally; model fitting remains full retraining.

Use chronological validation, causal preprocessing, properly matured target labels, and leakage-safe rolling feature windows.

Measure the complete workload of approximately 11 symbols and 10 models per PC, including data acquisition, preprocessing, training, inference, and persistence. Record training times, inference times, memory/resource use, failures, and missed deadlines.

Do not backdate forecasts or claim a model has been retrained when a cycle failed. Ensure recovery and idempotency after restart.

## 6. Create the New Ducketz Tab

Add a dedicated tab named:

**15-Minute Duckets**

Match the existing Ducketz application's dark navy visual design, compact grids, status bars, and green/red/neutral styling shown in the attached screenshot.

The new tab should include the following major components.

### A. Live Ensemble Consensus Visualization

This is a primary design requirement.

For each selected symbol, display all participating models in a vertical list.

Every row must contain the model name, a horizontal 0–100% probability bar, the exact probability percentage, and its model status/version. Include a visible 50% reference point.

Below the model rows, show:

- A large mean ensemble probability.
- The resulting not-down/down interpretation.
- Number of participating models.
- Reference open price/time.
- Target candle and closing time.
- Prediction issue time.
- Pending, scored, missing, or other applicable outcome status.

For example, ten model scores averaging 70.0% should produce a prominent **70.0% ensemble probability**.

This must be wired to genuine persisted model outputs, not hardcoded demonstration values. Do not confuse a model's predicted probability with its historical measured accuracy.

### B. Live Prediction Matrix

Build a multi-symbol prediction heatmap inspired by the existing Rolling Forecasts Prediction Pulse.

Display the newest 15-minute ensemble scores for the configured symbols, with filters for Atlas, Scout, symbol, and session.

Distinguish bullish/not-down, bearish/down, borderline, missing, stale, and pending information clearly.

### C. Forecast Accuracy and Evaluation

Track:

- Today's accuracy
- Rolling 20/50/100 predictions
- Seven-day and cumulative accuracy
- Individual model versus ensemble accuracy
- Results by symbol and PC
- Scored/pending/missing/unscorable counts
- Balanced accuracy, Brier score, and log loss
- Confidence calibration and model disagreement
- Prediction error correlations
- Training and inference latency

Accuracy must update only after the actual target candle has completed and its value is available.

A still-unresolved prediction is pending, not correct or incorrect. Any interim comparison must be explicitly labeled provisional.

### D. Market-Regime Diagnostics

My prior ensemble often handled large sell-offs and strong rallies well but performed less effectively during sideways, choppy, and small-magnitude price movements.

Make that behavior measurable.

Segment historical forecast performance by volatility, trend strength, market choppiness, realized price-movement magnitude, time of day, symbol, and session.

Report whether the ensemble handles large movements differently from relatively flat markets.

Retrospective evaluation may inspect realized price changes, but predictive features and live regime classifications must remain point-in-time causal.

### E. Operational Status and Execution History

Show PC health, data freshness, latest fetched candle, model retraining completion, model availability, inference timing, and remote telemetry freshness.

When existing authorized execution data is available, connect forecast identities to desired, adjusted, submitted, filled, and remaining quantities, along with orders and actual fills.

Keep forecast accuracy distinct from actual trading performance, realized P&L, transaction costs, and risk outcomes. Do not fabricate trade records.

## 7. Live Scoring and Historical Logging

Implement durable, auditable records for candles, features, training runs, model artifacts, individual forecasts, ensemble forecasts, final target outcomes, revisions, and existing order/execution relationships.

Persist timestamps in UTC while rendering Pacific time correctly with daylight-saving changes.

Support automatic scoring, no duplicate outcomes, late provider data, corrected prices, missing candles, market closures, and system restarts.

Provide daily reporting and historical analysis through the UI.

The monitoring interface must never claim remote updates are fresh when Scout or Atlas has stopped reporting.

## 8. Experimentation and Backtesting

Develop a point-in-time walk-forward evaluation system that replicates live prediction behavior.

Compare full retraining at 15 minutes against 30 minutes, 1 hour, 2 hours, 4 hours, daily, and weekly, using the same historical evaluation timestamps.

Also compare different rolling training-window sizes and individual-model versus ensemble performance.

Preserve 15-minute full retraining as the initial aggressive benchmark. Choose improvements based on genuinely out-of-sample results rather than training accuracy.

Track both forecast performance and eventual trade outcomes, but do not treat classification accuracy as equivalent to profitability.

## 9. Preserve Existing Two-PC and Account Architecture

Atlas runs the existing Gameplan stream as well as Atlas 15m. Scout runs its own 15m stream. All three ultimately share the same Schwab account.

Do not alter the existing Gameplan's horizons or nightly workflow.

Respect the design's Atlas-side shared purchasing-capacity authority, allocation ownership rules, unresolved cross-PC reservation protocol, and local Scout order-submission ownership.

Do not place SQLite authority databases on a shared network drive or assume existing host-local reservations work across PCs.

Use read-only telemetry for dashboard updates, separate from eventual order coordination.

The approved starter is a transparent technical-indicator calculus evaluated
for each symbol on each 15-minute cycle. EMA20/SMA20/VWAP20 distances and
four-bar momentum use that symbol's ATR14 normalization; simple RSI14,
Bollinger20, stochastic14, the issued probability and prior20 relative volume
complete nine clipped terms. BUY/SELL orientation is explicit. The score is
`clip(0.5 + sum(terms)/18, 0, nextafter(1,0))`; each symbol retains its own
inputs, version, contributions and immutable forecast snapshot. See the
[complete approved sizing contract](INTRADAY_15M_SYSTEM_DESIGN.md#3-approved-independent-technical-sizing).

The per-trade share reference is `0.015 * current account equity / fresh ask`
for BUY or `/ fresh bid` for SELL. Multiply by the score and round to nearest
whole shares with half-up ties; rounded equality is allowed. This later human
decision replaces the old normal-hourly reference. A scheduled Gameplan order,
horizon budget, protected cash portion or reserve buffer is not a prerequisite.
Desired size is fixed before capacity adjustment. Actual uncommitted cash or
eligible shares may clip it to a smaller whole quantity, including zero, while
the original forecast and intention remain scoreable.

15m SELLs use actual same-symbol shares across native lots and observed
unallocated stock, preserving every ownership reduction. They do not inherit
Gameplan's donor ceilings/pacing. Gameplan's own policy and stream fences remain
in force. The initial accepted 15m order works until terminal status or a newer
same-symbol forecast confirms cancellation; no Gameplan expiry/timeout or
automatic same-forecast repricing is adopted. Final-information/next-session
targets, future recipe tuning, any later repricing policy and the distributed
reservation protocol remain separately identified work.

## 10. Safety and Implementation Phases

The original proposal authorizes development, historical testing, shadow-mode
predictions, scoring and a read-only UI. The later human instruction extends
development toward the manual Atlas live execution path. It does not turn the
shadow runner into a live trader or start sessions, enable Scout execution,
change live schedules, or restart/deploy production workers. The manual Atlas
source uses the existing host-local native/account ledgers and prepared Schwab
transport. Only an explicit human invocation of `Start-15m-Trader.cmd` enters
its live path. `Check-15m-Readiness.cmd` remains read-only; it distinguishes
implemented source from runtime compatibility and fresh broker preflight.

The manual worker starts at the latest eligible completed candle, fits all
enabled models from scratch, publishes each symbol separately and skips missed
boundaries rather than trading a backlog. Forecast/model work is separate from
serialized account steps. Entry gates require current manual-session live
forecasts with observed availability and full ensembles, fixed unexpired
targets, the real market window, fresh identity/account/quote evidence, a
fenced shared lease, strict pre-16:45 submission and compatible loaded Gameplan
or independently verified absence. Old shadow/replay issues cannot submit.

Use already-authorized read-only historical/market-data access where permitted. Seek approval before initiating new live provider access or modifying credentials.

Implement in stages:

**Phase 1:** Inspect the repository, map existing modules, implement and test the model registry, exact target generation, Atlas shadow forecasting, and 15-minute retraining.

**Phase 2:** Implement delayed scoring, persisted performance tracking, and the new Ducketz tab, including the complete probability-bar consensus visualization.

**Phase 3:** Apply the same code and configuration scheme to Scout and establish safe read-only monitoring for both PCs.

**Phase 4:** Backtest and shadow-test performance, benchmark retraining schedules, and document what remains before any future trading integration.

**Phase 5:** Implement and offline-test the explicit Atlas manual session,
independent approved sizing, actual shared capacity clipping, durable intention
logging, exact-identity recovery and stream-specific cancellation. Source for
this phase exists; temporary-database checks do not prove live broker behavior.
Human launch and subsequent Atlas observation are distinct operating steps.

**Phase 6:** Apply reviewed common source on Scout for research/shadow and
read-only monitoring. Scout orders remain disabled until later human activation
and an implemented distributed Atlas reservation protocol; telemetry is not
that protocol.

Include tests for target equality, label maturity, lookahead leakage,
missing/duplicate candles, DST and the strict 16:45 submission cutoff,
new-intention reconciliation, model failures, immutable forecasts, crash
recovery and absence of broker-order side effects. Final-session/next-open
prediction targets remain separate unresolved contracts.

## 11. Expected Deliverables

Inspect the codebase first, explain the implementation approach, and then make the scoped development changes rather than delivering only a conceptual design.

When finished, report the actual changed files, configured model families, working versus unavailable models, completed tests and results, sample stored predictions and outcomes, UI demonstration, full 11-symbol-cycle timing, and any remaining unresolved decisions.

Do not report hypothetical accuracy as measured accuracy or mock UI data as live data.

**The goal is a working, observable, scientifically evaluable 15-minute multi-model ensemble that integrates cleanly into my existing Atlas/Scout/Ducketz architecture without disrupting the current Gameplan or enabling unauthorized broker execution.**
