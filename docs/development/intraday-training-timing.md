# Historical construction and exact operational replay

`intraday-training-timing-v1` binds a local recipe and every derived dataset,
fit and assessment to an explicit mode. Shared source does not migrate a
machine's private recipe. The migration helper returns a new candidate identity
and preserves models, scope, support gates and the previous recipe checksum.

`historical_research_v1` constructs a model now from legitimate historical
prices acquired by its actual fitting cutoff. Market timestamps order features,
labels, calibration and validation. Training targets must close before the
first calibration or validation reference, so overlapping target horizons stay
purged. Actual reference, feature dependency and target receipts remain in the
dataset and must not exceed the real construction/fitting cutoff. The date of
download need not precede an old calibration or validation boundary. Research
fold evidence records the market partition boundary separately from the actual
fit cutoff; it does not claim that these models or observations existed then.

Historical candles whose original receipt was never recorded remain explicitly
`assumed_historical_close`. That is a historical-price completion convention,
not a fabricated receipt. They can support present retrospective price research;
they cannot establish exact operational replay. Actual recovery timestamps and
all original raw and revision evidence remain unchanged.

`exact_operational_replay_v1` additionally requires actual local possession by
the original decision/fitting time. Assumed historical receipts are rejected;
late feature dependencies, references arriving outside their original decision
window and labels received after the replay fit boundary are unavailable.
Recovered history does not become retroactively possessed. Unversioned recipes
retain `legacy_receipt_gated_v1` interpretation for compatibility; that legacy
identity alone is not a certification of exact replay. Existing forecasts and
their saved replay evidence are never rewritten.

Research price features are organized by completed market intervals. Context
joins retain their own publication/version and source rules and use the
reference close, never a late candle-download timestamp as an invented decision
time. Current books and fundamental snapshots cannot populate old candles.
Event dates and fiscal period dates alone are not publication evidence.

Imputation, scaling and optional-feature selection fit only the purged base
partition. Calibration and inference cannot activate a context column absent
or constant in that base. A new context observation can therefore be correctly
unavailable to a base estimator while its reference is still inside the chosen
calibration tail. This is distinct from the repaired accidental delay imposed
on recovered price history. Calibration-window changes require separate local
development assessment; every row/date/class/performance gate remains intact.

Preparation fits, development assessments, independent validation and trading
adoption are separate outcomes. A successful fit does not qualify a model or
authorize trader startup. Holdout history and failed support remain durable.
