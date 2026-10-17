# Observed Gameplan technical inputs

The explicit `observed-multi-source-stock-v2` price policy with archive history
uses `xnas-archive-observed-technicals-v2` feature selection. Legacy archive and
canonical policies retain their existing feature contract. Selecting this new
contract requires a new model assessment; training success alone does not imply
promotion or qualification. Per-PC model recipes and symbols remain local.

`ml/gameplan_observed_technicals.py` clears the complete `mr__`, `bp__`, `bar__`
and `life__` namespaces before rebuilding technical values. Verified native
XNAS hourly history supplies the main population; verified native minute tails
can supply observed hourly aggregates beyond native-hour coverage. Nonpositive
volume, invalid OHLC and synthetic or source-empty markers cannot contribute
to aggregation or rolling calculations. An empty covered hour stays absent.
A successful source request does not prove that no market transactions occurred.

Existing market-regime, session-aware breakout-pressure and bar-shape formulas
and parameters are preserved. Lookbacks count genuine observations, not elapsed
hours. Known split, provider-quality and unverified-coverage boundaries reset
recursive state. At least sixty actual past observations are required before
the mature calculation outputs are eligible. Appending future bars cannot
change eligible historical values. Persisted raw metric values retain their
existing scale. The complete declared technical namespace can gain previously
missing fields under this explicitly new contract.

The selected hour must belong to the source session and satisfy the decision
cutoff. Historical feature availability is reconstructed as interval end plus
five minutes; it is an explicit assumption, not a contemporaneous receipt.
Actual evidence acquisition time is recorded separately. Historical lifecycle
fields remain missing until clean, genuinely timed calculation evidence exists.

New runs bind `observed-technical-sources.parquet` and its checksum in the
archive report and publication manifest. Rows retain the contract, selected
bar and availability clocks, segment start, genuine history count and complete
recursive raw-input hash ancestry. Reader validation checks this evidence;
champions from a different feature contract cannot silently score it. Runtime
receipts and model-review bindings include the actual calculation modules.

Observed-price recovery chooses complete same-source endpoint pairs. FMP
five-minute observations remain five-minute bars and are never relabeled as
native minutes. Stats retains the original outcome event deadline while using
the actual review time to admit later acquisition receipts. Old saved forecasts,
Loop B files, model artifacts and operating datasets are not rewritten.

The shared derived-hour producer now filters invalid observations before
projection and no longer emits previous-close values for empty request-covered
hours. This prospective repair does not cleanse old stored derived files;
consumers requiring clean history must use a versioned reconstruction.

Offline checks cover source identities and hash validation, causal clocks,
warm-up/future independence, reset boundaries, empty and flagged observations,
source-contract compatibility, saved lineage, historical Stats timing, and
observed same-source target construction. These tests do not call providers,
train operational models, place orders or verify a running trader.
