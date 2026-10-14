# Independent 15-minute model supervision

The local 15-minute trader prepares its own models on weekdays at 01:00,
01:30, 02:00, 02:30, 03:00 and 03:30 in `America/Los_Angeles`. The primary
schedule is one Codex automation using `gpt-6.1-sol` with `ultra` reasoning,
as requested by Jeremy. Its agent runs `ml.intraday.supervisor`, inspects the
recorded result and repairs actual failures within the same preparation scope.
The final scheduled wake is 03:30; it is not a deadline for running work.
Preparation continues until unfinished work is complete, including after 04:00.
Only Jeremy's actual explicit request authorizes stopping nightly work.

The schedule prepares models for Jeremy's manual session start. It never
launches, restarts or enables a trader, places orders, changes a manual stop,
or requires another trader, the peer PC or Gameplan production to be ready.
Atlas and Scout supervise their own profile/watchlist and use their own
execution records. There is no combined 15-minute trader or shared execution
ledger. See [Independent trading systems](independent-traders.md).

## Research and activation

A confidence of 100% on one forecast is a reason to investigate calibration;
it does not by itself measure overfitting. The supervisor records Brier score,
log loss, balanced accuracy, probability saturation and the difference between
training and chronological validation performance. It also records the most
recent completed exchange session's local saved forecast outcomes and activity.
Missing observed receipts remain explicit. Historical outcomes reconstructed
from same-source candles are labelled research evidence and do not rewrite
live receipts or invent fills or profitability.

The current feature lane uses OHLCV and indicators derived from OHLCV. Each
wake refreshes the existing authenticated minute-data source through the
established continuation, overlap and idempotent upsert implementation. It
preserves native raw records and consumes only completed candles and mature
labels available at the frozen training cutoff. There is no external options,
macro, broker-account or peer-training input in this lane. A future feature
must acquire and record its own causal availability before it can participate.

The default research grid attempts 21 configurations covering every one of the
16 implemented exact-target families: logistic, Gaussian NB, KNN, LDA, QDA,
random forest, extra trees, histogram/gradient/AdaBoost boosting, XGBoost,
LightGBM, CatBoost, Ridge, Passive Aggressive and MLP. Optional dependencies are
included in the research attempts; an unavailable dependency is recorded as a
failure, never silently skipped or replaced. Install the `ml-tree` extra for the
three optional boosting libraries. Multiple regularized logistic, Gaussian,
forest and MLP configurations compete within their genuine families.
Candidates are compared using expanding chronological validation folds. Label
windows and label availability are purged at partition boundaries; probability
calibration uses a separate chronological partition. A final holdout is reported
after candidate membership is fixed and cannot change that choice. Repeated
wakes retain that night's holdout boundary so already inspected holdout rows
cannot move into that night's search folds.

The selector ranks probability quality against a training-only class-frequency
baseline and records each candidate's success, exception or rejection reason.
The default selection limit is 16 instead of four, retaining one qualifying
configuration per family. A local `selection.max_selected` or explicit
`candidates` override remains explicit. More candidates do not guarantee a
better ensemble: validation advantage, overfit gaps and fold stability still
determine statistical selection. Catalog coverage, missing dependencies,
unsupported adapters, fit failures, validation rejections, duplicate-family
exclusions and configured limits are saved with each report.
It activates selected model specifications atomically per symbol. If there is
no qualifying selection, insufficient history, a fetch failure or another real
error, the previous complete selection survives and the error remains in the
run report. Other symbols can proceed. This is statistical model selection,
not an operational approval or trader-start prerequisite.

The forecast runtime reads the locally selected specifications, fits them
fresh at its own causal forecast cutoff and records selection provenance with
its saved forecasts. Research objects are not reused as fitted live models.
The supervisor does not claim that selected models guarantee profits.

Fourteen additional neural family names are recorded as
`adapter_not_implemented`. Legacy network code and installed Torch do not
constitute a genuine exact-target 15-minute adapter. Reuse requires causal
sequence windows, training-only preprocessing, exact matured labels and
probability/artifact contracts; existing checkpoints are not substituted.

## Saved display and performance cohorts

The 15-Minute Duckets Model research tab shows the local saved nightly report:
all candidate results, validation scores, selection/exclusion reasons,
unsupported families and report-only holdout scores. Those research rows are
distinct from the consensus bars, which retain the models actually fitted for
the selected saved forecast. Older reports lacking catalog coverage remain
labelled as older evidence. Missing or damaged reports never trigger training
or silently masquerade as a new successful run.

Performance displays default to observed outcomes from forecasts issued before
target open. The score cohort is applied before the requested window and to
accuracy, calibration, correlations and regime diagnostics. Forecasts issued
during the target candle, at/after its close, synthetic outcomes and unknown
timing remain selectable separately; All saved outcomes retains the historical
combined view. Consensus and the prediction matrix retain all original rows.
Timing comes from the saved timestamps, not a mutable late-issue flag.

The majority-class baseline describes the same scored cohort and is not a
forecast or a return. Small timely samples and late acquisition/recovery remain
visible limitations. Removing a model-count cap cannot itself fix missing or
late inputs, and a confidence percentage is not observed accuracy. No display
or statistical selection changes a manual trader start/stop or execution state.

## Local bindings and evidence

The command uses this PC's existing interpreter, datastore, coordination
profile and watchlist. Tuning configuration is optional JSON with
`source_dataset`, `lookback_days`, `synthetic_policy`, `selection` and
`candidates` fields. `selection` holds `SelectionConfig` values;
`candidates` holds `ModelSpec` definitions. Shared defaults remain source;
machine paths, symbols, native IDs and live selection artifacts remain local.

```text
PYTHON -B -m ml.intraday.supervisor --datastore DATASTORE --profile LOCAL_PROFILE --watchlist LOCAL_WATCHLIST --config LOCAL_CONFIG --log-dir LOCAL_LOG_DIRECTORY
```

The durable local paths under `DATASTORE/intraday/model-supervision` are:

- `active.json`: complete per-symbol selected specifications and provenance.
- `selection-state.json`: each night's retained holdout boundaries.
- `logs/events.jsonl`: appended run, refresh and symbol outcomes.
- `logs/RUN_ID.json`: complete run report, including candidate scores and errors.

`--log-dir` replaces the default logs directory. The CLI also prints its run
report and returns a failure result for unfinished errors. Runtime and
datastore locks prevent overlapping writes while preserving the existing run;
they do not stop a trader or consult cancellation records. Logs, raw data,
execution records and fitted models stay on this PC. Shared source publication
follows the installed coordination release and does not delay preparation.

[Reusable scheduled prompt](intraday-model-supervisor-prompt.txt) contains local
binding tokens. Resolve them using the local profile and existing installation
before saving the Codex automation; do not schedule unexpanded tokens. Use the
native automation tool to create or update the owned automation and preserve
its ID on retries. Record its selected model, reasoning effort, timezone,
cadence, enabled state and local bindings in ignored installation evidence.

## Optional Windows scheduler alternative

`tools/register_intraday_model_supervisor.ps1` prepares a native Windows task
for environments that choose direct deterministic Python scheduling instead
of a Codex automation. It does not select an AI agent model. Use only one
active scheduler for this purpose on a PC. The GPT-6.1-Sol/ultra Codex schedule
is the primary installation requested on October 14, 2026; the native helper
is provided as a portable alternative and is not also registered.

Supply the owned task name and explicit local `-Repository`, `-Python`,
`-Profile`, `-Watchlist`, `-Datastore`, `-Config` and `-LogDirectory` paths.
Prefer the existing environment's `pythonw.exe` when using native scheduling.
It must be the environment with the application's model dependencies.
`-Plan -XmlPath ABSOLUTE_PATH` saves a reviewable XML definition without
registering or running anything; `-ValidateOnly -XmlPath ABSOLUTE_PATH`
checks a saved definition without scheduler calls.

Native installation requires the host's existing `Pacific Standard Time`
timezone and uses six floating weekday calendar boundaries that follow its
daylight-saving rules. The helper does not change the host timezone. It uses
the existing interactive account with limited privilege, so that account must
be signed in. It sets `IgnoreNew` to preserve a running invocation,
`StartWhenAvailable` for missed work and `PT0S` for unlimited execution time.
Idleness, battery transitions and a later wake do not terminate existing work.

Before an owned existing task is updated, its original XML is preserved under
`LOG_DIRECTORY/task-rollback`. The helper refuses a task name belonging to
another purpose or another existing task with the same supervisor purpose.
It reads back and validates registered XML, without starting a task for
verification. Cadence, manual-start boundaries and rollback preservation are
checked by offline PowerShell fixtures in
`tests/test_register_intraday_model_supervisor.py`; fixtures replace native
scheduler APIs and make no provider, training, broker or task-start calls.
