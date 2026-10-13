# Codex: Implement 15-Minute Duckets — Continuously Retrained Multi-Model Ensemble

Jeremy's October 13, 2026 standing direction defines three independent traders. Atlas runs the combined 22-symbol Gameplan trader and Atlas's local eleven-symbol 15-minute trader. Scout runs its own eleven-symbol 15-minute trader. Each trades and logs its own activity with its own local state. No trader checks, verifies, reconciles, combines, or depends on another trader.

Only the two nightly eleven-symbol Gameplans are synthesized. Use one actual account cash/stock balance, publish the same combined Gameplan and Stats on both PCs, and prepare the traders every night under permanent authorization. Atlas may synthesize directly and return the completed result to Scout without an acceptance wait. The human manually starts each trader. See [Independent trading systems](../development/independent-traders.md).

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

Jeremy's October 13, 2026 standing direction defines three independent traders. Atlas runs the combined 22-symbol Gameplan trader and Atlas's local eleven-symbol 15-minute trader. Scout runs its own eleven-symbol 15-minute trader. Each trades and logs its own activity with its own local state. No trader checks, verifies, reconciles, combines, or depends on another trader.

Only the two nightly eleven-symbol Gameplans are synthesized. Use one actual account cash/stock balance, publish the same combined Gameplan and Stats on both PCs, and prepare the traders every night under permanent authorization. Atlas may synthesize directly and return the completed result to Scout without an acceptance wait. The human manually starts each trader. See [Independent trading systems](../development/independent-traders.md).

The approved nine-term technical recipe remains `clip(0.5 + sum(terms)/18, 0, nextafter(1,0))`, with each symbol's causal EMA20/SMA20/VWAP20 distances, four-bar momentum, ATR14 normalization, simple RSI14, Bollinger20, stochastic14, issued probability and prior20 relative volume. The per-trade reference is `0.015 * current equity / fresh BUY ask or SELL bid`; multiply by the score and round half-up.

Desired size is independent of Gameplan instructions. Actual usable cash or available shares can clip the submitted quantity, including zero. Preserve the forecast and own order history. No shared native ledger, Gameplan source check, remote grant or cross-trader verification is involved.

## 10. Safety and Implementation Phases

Prepare both local 15-minute implementations every night under permanent standing authority. Source adoption and routine repair do not need repeated approval. Each trader starts only from the human's manual launcher and preserves the local stop control.

Preserve current causal live forecasts, fixed targets, the full enabled ensemble, own-symbol sizing, actual broker account capacity, current quotes, market window and strict pre-16:45 Pacific submission cutoff. Do not trade an old shadow/replay issue or a backlog. An unresolved own submission retains its true identity and is not guessed or resent.

Offline regression tests cover target equality, mature labels, lookahead exclusion, missing/duplicate candles, DST, model failures, immutable forecasts, own cancellation and crash recovery. They are software checks, not reciprocal trader verification. Special next-open targets and later model/recipe research remain separate work.

## 11. Expected Deliverables

Inspect the codebase first, explain the implementation approach, and then make the scoped development changes rather than delivering only a conceptual design.

When finished, report the actual changed files, configured model families, working versus unavailable models, completed tests and results, sample stored predictions and outcomes, UI demonstration, full 11-symbol-cycle timing, and any remaining unresolved decisions.

Do not report hypothetical accuracy as measured accuracy or mock UI data as live data.

**The goal is a working, observable, scientifically evaluable 15-minute multi-model ensemble that integrates cleanly into my existing Atlas/Scout/Ducketz architecture without disrupting the current Gameplan or enabling unauthorized broker execution.**
