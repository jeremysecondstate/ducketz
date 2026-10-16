# Local 15-minute model policy

Jeremy's direct October 16, 2026 instruction keeps Atlas and Scout's model
repairs and statistical tuning specific to the producing PC and applicable
symbols. Do not install Scout-specific model tuning on Atlas or Atlas-specific
tuning on Scout merely because the shared source was published or adopted.
This narrows earlier blanket peer-adoption language for 15-minute model policy.

## What stays local

Each PC owns its selected model specifications, candidate grid and parameters,
calibration settings, training windows, synthetic-data treatment and selection
criteria. Selection should reflect each symbol's own causal evidence; do not
assume that every symbol on one PC has the same liquidity or data coverage.
Fitted models, data, selection reports, account state and execution receipts
remain private under the existing rules.

Reusable correctness and observability repairs may remain shared. Inspect their
effect on local model behavior before adoption. In particular, changes to shared
default parameters, preprocessing, labels, candidate grids or library behavior
are not automatically an applicable statistical change for both PCs. Use an
explicit local policy for symbol-specific tuning rather than changing a global
default to address one PC's results.

The local supervisor accepts explicit candidates and selection settings. Pin
the existing effective parameters when making this policy explicit; do not
reinterpret pinning as new tuning or silently rewrite historical selection IDs.
New selections are produced through the normal supervisor and retain that
night's holdout boundary. Explicit settings pin the defaults currently recorded;
new parameters and shared implementation or library changes still need review.

## Evidence for sparse inputs

High displayed confidence alone does not diagnose overfitting. The current
target includes equality: a target close equal to the reference open is
"not-down". Confirmed no-trade continuity can therefore produce a positive
label without an observed price increase.

Compare predictions issued before the target opens and their observed outcomes
separately from predictions issued during or after the target, synthetic
outcomes and historical reconstructions. Inspect calibration, chronological
validation gaps, class balance and sample size. Preserve original records and
their actual receipt times; no artificial candles or profitability claims.

Review input coverage by symbol and actual observed-minute content. Fully
observed historical training rows do not establish model support for mostly
synthetic live inputs. Record that limitation and investigate with causal
training/validation evidence; do not repeatedly tune against an inspected
holdout. Feed-specific missing intervals also do not prove no trading across
the entire market. Do not switch datasets or buy coverage as a side effect of
this analysis.

## Saved diagnostics

`tools/intraday_model_diagnostics.py` reads an exact local supervisor report and
the same actor's saved prior-session forecasts/outcomes. It writes a separate
private JSON diagnostic with timing/outcome cohorts, input coverage and training
population information. It does not fit, select, fetch, rewrite the supervisor
report, alter trading controls or call providers/brokers. Use the existing
Python environment and pass `--datastore`, `--profile`, `--report` and a private
`--output` path. Its output is observational and creates no operating gate.

Nightly preparation remains independent and continues under standing human
authorization. No peer response, acceptance round or source-publication stage
is a prerequisite. Jeremy retains every trader's manual start/stop controls.
