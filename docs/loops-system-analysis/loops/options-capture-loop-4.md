# Options Capture / logical Loop 4

## Identity

- Canonical name: Options Capture runtime
- Logical aliases or numbering: logical Loop 4; startup owner 5
- Runtime entry point: `python -m datafetching.options_runtime`
- Owning package: `datafetching` with `options` publication/normalization contracts
- Classification: Independent production loop
- Scheduling mechanism: recurring quarter-hour supervisor with one owned live OPRA receiver, one daily completed-cursor history catch-up, bounded internal barrier polling, and pending-capture reconciliation
- Cadence and phase: every 15 minutes at UTC phase +6 minutes; 45-second default Pricing-barrier wait
- Lock or single-writer mechanism: `.ducketz-options-writer.lock`, shared by all committed option-snapshot publications
- Primary code evidence: **Confirmed.** `options/databento_live.py`, `datafetching/options_runtime.py`, `datafetching/databento_opra_history.py`

## Purpose

**Confirmed:** Options Capture owns acquisition and immutable publication of provider-neutral option-chain evidence at the calendar target. Default production mode constructs one canonical Databento OPRA L1 adapter before recurrence; per target it publishes validated OPRA using independent evidence clocks or, only after bounded OPRA unavailability, separately labeled Schwab fallback. OPRA capture does not wait for Loop A, although Loop 3 cannot price without exact Loop A authority. If a Schwab fallback occurs while readiness is unavailable, the runtime makes one durably claimed request inside the causal window, seals the response under non-production pending authority, and reconciles it only after exact readiness is provable. `options/databento_live.py:33`, `datafetching/options_runtime.py:360`, `datafetching/options_runtime.py:369`, `datafetching/options_runtime.py:384`, `datafetching/options_runtime.py:452`, `options/pending_capture.py:264`

**Confirmed OPRA scope:** the concrete live implementation subscribes to definitions and `OPRA.PILLAR` `cbbo-1s` for exactly `AAPL.OPT AMZN.OPT GOOG.OPT MU.OPT NVDA.OPT SNDK.OPT`; `SPY` is rejected. One reconnecting client and bounded buffers are shared across symbols/targets. Historical scope is separate: `datafetching.options_history` bootstraps each new parent symbol across all Standard schemas, while the optional one-shot `datafetching.databento_cold_start` can populate the same OPRA contract alongside isolated CME/US-equity archives. Both hand verified OPRA scopes to this supervisor through v5 symbol/schema cursors. `options/databento_live.py`, `datafetching/options_history.py`, `datafetching/databento_cold_start.py`, `datafetching/options_runtime.py:110`

**Confirmed historical policy:** initial lookbacks are 5 days (`ohlcv-1s`), 100 days (`ohlcv-1m`), 2,000 days (`ohlcv-1h`), 5,000 days (`ohlcv-1d` and `definition`), one month (`cmbp-1`), and six months (all remaining Standard schemas). Catch-up overlaps are 1/2/5/10 days for those four OHLCV frequencies and three days for every other schema. The recurring runtime never bootstraps a missing cursor; it reports the required one-time command instead. `datafetching/options_runtime.py`, `docs/datafetch-ml/options-opra-history.md`

**Confirmed non-ownership:** it does not declare bars ready, value contracts, fit directional or strategy models, or make orders. A Pricing target result is sequencing evidence, not permission to fabricate or alter option data. `datafetching/pricing_barrier.py:38`, `options/publication.py:105`

## Inputs

| Input or dataset | Producer/source | Physical path or interface | Key fields and semantic values | Clock/freshness/causality rules | Required or optional | Evidence |
|---|---|---|---|---|---|---|
| Target decision and exact bar readiness | Loop A | target-scoped readiness receipt or exact persisted completed bars | exact quarter-hour, per-symbol decision bar/close/provider/timeframe, decision timestamp and source file | Calendar target scopes OPRA; readiness clock cannot be future; discovery mode reconstructs exact clocks without claiming a receipt | Required for downstream Pricing and Schwab commit; not required for causally clocked OPRA commit or pending evidence | **Confirmed.** `datafetching/options_runtime.py:117`, `datafetching/options_runtime.py:266`, `datafetching/options_runtime.py:289` |
| Latest complete Loop A cutoff and daily bars | Loop A | complete-cycle pointer plus normalized price history | `finished_at` bounds regime evidence; adjusted-close history supplies causal 20-day realized volatility and split state | Regime inputs must be available no later than request; no post-request Loop A evidence | Required for committed feature lineage; feature values may be missing with status | **Confirmed.** `datafetching/options_runtime.py:335`, `datafetching/options_runtime.py:461`, `options/features.py:288` |
| Target Pricing outcome/barrier | Active Pricing | verified target outcome/receipt | barrier `VERIFIED`, `MISSING`, or `TIMED_OUT`; Pricing terminal status, prediction row count, publish/observe/request clocks, receipt checksum; `prospective_credit_allowed` | Wait at most configured 45 seconds; credit only when Pricing authority and observation precede the request and predictions exist | Optional for capture; mandatory only for prospective Pricing-before-capture credit | **Confirmed.** `datafetching/pricing_barrier.py:18`, `datafetching/pricing_barrier.py:38`, `datafetching/pricing_barrier.py:77` |
| Canonical OPRA snapshot | Owned `DatabentoOpraLiveAdapter` | shared `OPRA.PILLAR` definition + `cbbo-1s` live buffers returned through `OptionMarketDataAdapter` | active standard contract definition/CFI, CALL/PUT, strike, expiration, 100 multiplier, exercise/settlement, publisher 30, final BBO and sizes, interval/event/provider/local clocks | Exact non-null provider/dataset/schema/symbol/target; definition provider receipt/effectiveness and contract activation by target, definition event no later than provider receipt, and local visibility by publication; final noncrossed complete BBO strictly pretarget and ≤1,200 s stale; target/contract/definition/mapping buffers are bounded and require a target watermark; divergent/corrupt evidence fails closed | Canonical production lane; adapter construction is mandatory in `opra-canonical` mode | **Confirmed.** `options/databento_live.py:268`, `options/databento_live.py:338`, `options/databento_live.py:413`, `options/databento_live.py:549`, `options/databento_live.py:563`, `options/snapshot.py:297`, `options/snapshot.py:541` |
| Schwab option chain | Lazily constructed Schwab session | `get_option_chain_snapshot` | contract symbol, expiration, DTE, strike, bid/ask/mark, volume/OI, IV, Greeks, theoretical/intrinsic values and underlying quote | One request per unclaimed symbol/target; quote cutoff is request start; response time becomes first availability; persistent bounded provider retry | Explicit transient-OPRA fallback/broker lane, or explicit `schwab-only-compatibility` mode | **Confirmed.** `datafetching/options_runtime.py:384`, `datafetching/options_runtime.py:452`, `datafetching/options_runtime.py:466`, `datafetching/options_runtime.py:477`, `datafetching/options_runtime.py:650`, `options/snapshot.py:739` |
| Earlier committed/pending state | This loop | snapshot pointers/receipts and `options/pending-captures/schwab/<target>/<symbol>/` | natural target claims, request/response clocks, raw payload checksum, `REQUEST_STARTED`, `PENDING_READINESS`, reconciled/expired/failed state | Existing committed target skips provider; pending claim is durable before its sole request; request must be target through target +1,200 seconds | Optional on bootstrap; authoritative when present | **Confirmed.** `datafetching/options_runtime.py:162`, `options/pending_capture.py:100`, `options/pending_capture.py:118`, `options/pending_capture.py:147` |

## Processing and decisions

Before the first target cycle of a UTC date, the supervisor attempts one bounded catch-up for every requested symbol/schema with a valid history cursor. Current writes are v5 and include the exact lookback policy, requested start and completion boundary; a legacy v4 cursor is accepted only when its policy exactly equals the old schema-specific bootstrap policy. Checksum-valid partitions are reused; capacity-blocked or failed scopes are reported without deleting completed work. This maintenance does not make a missing symbol ready and does not turn an empty directory into fetch evidence.

1. **Confirmed:** reconcile prior pending captures, derive the only eligible target, and skip symbols already committed or claimed. Closed-market discovery may refresh the latest eligible target but does not relabel it as a new calendar target. `datafetching/options_runtime.py:108`, `datafetching/options_runtime.py:117`, `datafetching/options_runtime.py:162`
2. **Confirmed:** poll the target Pricing authority for a bounded interval. The internal `while` in the barrier is a wait loop, not a runtime owner; timeout returns explicit metadata and capture continues. `datafetching/pricing_barrier.py:77`, `datafetching/pricing_barrier.py:98`, `datafetching/pricing_barrier.py:124`
3. **Confirmed:** read exact Loop A readiness, then independently read the owned OPRA buffer for the calendar target. OPRA target selection uses its own definition/quote clocks and is never placed in the Schwab pending namespace. If readiness is absent and OPRA is transiently unavailable (or compatibility mode is explicit), durably claim a pending target before the one Schwab request. `datafetching/options_runtime.py:268`, `datafetching/options_runtime.py:360`, `options/pending_capture.py:118`
4. **Confirmed:** validate exact OPRA identity and normalize received/effective, activated-by-target definitions plus final strictly pretarget quotes. Only `OptionProviderUnavailable` crosses into labeled Schwab fallback. Definition, identity, duplicate, clock, schema, or corruption failures reject the target with no broker substitution. `datafetching/options_runtime.py:369`, `datafetching/options_runtime.py:374`, `datafetching/options_runtime.py:384`, `datafetching/options_runtime.py:408`, `datafetching/options_runtime.py:428`, `options/snapshot.py:122`
5. **Confirmed:** for Schwab, fetch once, normalize contract/quote fields, compute option-quality features and causal realized-volatility context, then either seal the payload pending readiness or publish it under the exact decision clock. `datafetching/options_runtime.py:424`, `datafetching/options_runtime.py:437`, `datafetching/options_runtime.py:454`
6. **Confirmed:** publication validates coherent raw/contracts/feature keys, writes them to a staging directory, checksums each output, commits the directory and atomically advances the provider pointer. Identical retries reuse; divergent retries fail closed. `options/publication.py:120`, `options/publication.py:139`, `options/publication.py:163`, `options/publication.py:187`
7. **Confirmed:** reconciliation verifies pending request/capture, exact later readiness and causal expiry before invoking the same commit path. Pending response polling/reconciliation is owned internal state, not another production loop. `options/pending_capture.py:204`, `options/pending_capture.py:264`, `options/pending_capture.py:375`

## Outputs

| Output | Consumer(s) | Physical path or interface | Key output values and meanings | Publication/authority rules | Evidence |
|---|---|---|---|---|---|
| Provider-neutral immutable snapshot | Active Pricing; Directional Loop B; Strategy | `stocks/<symbol>/options/snapshots/<provider>/<target>/` | `raw.parquet`, `contracts.parquet`, `option-quality.parquet`; exact target, provider/evidence lane/fallback, definition/event/provider-receipt/provider-send/local-receipt/publication clocks, contract semantics, bid/ask/mid, optional IV/Greeks/liquidity, Pricing-barrier proof | Natural key `(provider, symbol, target_snapshot_for)`; coherent nonempty frames; receipt/checksums; identical retry only; writer lock | **Confirmed.** `options/publication.py:23`, `options/publication.py:30`, `options/publication.py:92`, `options/snapshot.py:423` |
| Per-provider current pointer | Pricing, B, Strategy/UI readers | `stocks/<symbol>/options/latest/<provider>.json` | provider/dataset, target, availability, snapshot path and receipt identity | Atomic and monotone in target/availability; older/equal authority does not displace current | **Confirmed.** `options/publication.py:75`, `options/publication.py:814`, `options/publication.py:842` |
| Compact option feature row | Directional Loop B and indirectly Strategy | snapshot `option-quality.parquet` | ATM implied move/IV, realized-vol comparison, term/skew/smile, OI/volume ratios, parity, spread/staleness/coverage and quality pass/status | Derived only from causal rows; quality pass requires coverage, freshness, noncrossed/nonlocked positive quotes and no intrinsic violation | **Confirmed.** `options/features.py:186`, `options/features.py:199`, `options/features.py:214`, `options/features.py:285` |
| Pending capture authority | This loop only until reconciliation | `options/pending-captures/schwab/<target>/<symbol>/` | exact scope and target, request/response clocks, sealed raw payload/checksum, Pricing barrier metadata, `PENDING_READINESS`/reconciled/expired/failed states | Not a production snapshot and cannot authorize actions; immutable response; commit only after exact readiness; terminal expiry prevents late relabeling | **Confirmed.** `options/pending_capture.py:100`, `options/pending_capture.py:179`, `options/pending_capture.py:264` |
| Cycle/failure result | operator/error authority | console result and datastore failure records | published/failed/skipped; OPRA/Schwab calls/fallbacks; barrier and pending/reconciled/expired counts | Per-symbol failure isolation; successful symbols remain committed | **Confirmed.** `datafetching/options_runtime.py:554`, `datafetching/options_runtime.py:484` |
| Canonical historical partitions and cursors | Active Pricing; Strategy; operator health | `market-data/databento-opra/OPRA.PILLAR/schema=<schema>/date=<date>/bucket=<bucket>/`, `state/symbol-history/`, `health/current.json` | provider DBN, normalized Parquet, distinct manifest/receipt, row/timestamp/null/duplicate/checksum validation; v5 lookback/request/completion/optional bootstrap-manifest cursor | Atomic publication; cursor only after nonempty verified synchronization; a missing/invalid cursor remains bootstrap-required | **Confirmed.** `datafetching/databento_opra_history.py`, `datafetching/options_runtime.py:1018`, `datafetching/options_runtime.py:1107` |

## Direct loop relationships

### Upstream

- **Confirmed:** Loop A provides exact commit clocks, readiness and regime/realized-volatility evidence. `datafetching/options_runtime.py:270`, `datafetching/options_runtime.py:335`
- **Confirmed:** Pricing supplies the optional bounded barrier proof; capture does not block indefinitely or require a successful prediction. `datafetching/options_runtime.py:250`, `datafetching/pricing_barrier.py:52`

### Downstream

- **Confirmed:** Pricing reads earlier chains as prediction inputs and the earliest eligible later-target chain as an outcome. Prediction publication precedes the outcome quote/receipt, and the later target is retained rather than backdated. `ml/option_pricing/causal.py:963`, `ml/option_pricing/causal.py:1215`, `tests/test_option_pricing_core.py:536`
- **Confirmed:** Loop B joins compact `opt__` option-quality features by point-in-time availability. `ml/rolling_materialization.py:614`
- **Confirmed:** Strategy selects exact entry and exit chain receipts and uses contract rows to construct and evaluate candidates. `ml/strategy_selection/runtime.py:240`, `ml/strategy_selection/runtime.py:383`, `ml/strategy_selection/runtime.py:396`

### Timing and control relationships

**Confirmed:** intended phase is +6 minutes, after Pricing +1 and B +5. The B relationship is timing only in the startup/flowchart; Options consumes no B artifact. Pricing’s 45-second barrier is optional for capture but its proof determines whether an option snapshot can receive prospective Pricing-before-request credit. `docs/datafetch-ml/current_start_command:94`, `docs/datafetch-ml/current_start_command:160`, `docs/datafetch-ml/current_start_command:188`, `datafetching/pricing_barrier.py:52`

## Prediction contribution

| Prediction family | Contribution | Explanation and exact causal chain |
|---|---|---|
| Directional horizon predictions | Indirect | committed chain → option-quality `opt__` feature row → Loop B materialization → calibrated horizon probability. `options/features.py:214`, `ml/rolling_materialization.py:614`, `ml/runtime_pipeline.py:493` |
| Option-pricing predictions | Indirect | earlier committed chain + later observed chain → Pricing sample/prediction input and realized evaluation/model evidence. `ml/option_pricing/consumers.py:372`, `ml/option_pricing_runtime.py:466` |
| Options-strategy predictions | Indirect | exact entry/exit chains → construct candidates and historical outcomes → fit/score profitable-outcome probability and rank. `ml/strategy_selection/runtime.py:240`, `ml/strategy_selection/runtime.py:351`, `ml/strategy_selection/runtime.py:311` |

**Roll-up classification: Both.** It has direct evidence paths into directional predictions and both option-related prediction types, while publishing none of those final authorities itself.

## Failure and degradation behavior

- Missing `DATABENTO_API_KEY` or live-adapter startup failure stops canonical
  production mode before recurrence; it cannot silently become Schwab-only.
- Only bounded `OptionProviderUnavailable` permits the labeled Schwab fallback.
  Definition, identity, duplicate, clock, schema or integrity failures fail the
  affected target closed without broker substitution.
- A missing/timed-out Pricing barrier never blocks capture; it records no
  prospective Pricing credit. Causally clocked OPRA can commit without Loop A
  readiness. A Schwab response without readiness remains checksum-sealed in
  pending authority and is either reconciled inside 1,200 seconds or expires.
- Snapshot failures are isolated per symbol. Verified symbols remain committed,
  and pointer/checksum verification prevents partial staging from becoming
  current.
- Daily history catch-up isolates capacity/provider errors by symbol/schema and
  never creates a missing cursor. A `bootstrap required` result delegates to a
  one-shot bootstrap rather than expanding the recurring owner’s scope.


## Accuracy and efficiency relevance

- One shared OPRA definitions/`cbbo-1s` transport serves all six parents; target
  selection uses the final valid pretarget BBO and bounded buffers rather than
  opening six subscriptions per cycle.
- Immutable natural identity, distinct market/provider/local/publication clocks,
  definition activation checks and exact receipt checksums prevent replay,
  duplicate divergence and backdated prospective evidence.
- Daily schema-specific overlap plus canonical deduplication repairs recent
  history without repeating the large initial bootstrap.
- Actual OPRA coverage, fallback share and option-feature/model lift require
  current health, receipt and consumer-usage evidence.


## Conflicts, gaps, and uncertainty

- `schwab-only-compatibility` remains executable for explicit compatibility,
  but it is not the supported canonical production mode and does not create a
  second owner.
- The all-dataset cold start is a one-time maintenance coordinator. It uses the
  canonical history `state/sync.lock` and publishes a v5 cursor handoff, but
  never takes `.ducketz-options-writer.lock` or publishes snapshots/readiness.
- A valid cursor proves a verified completion boundary, not that every current
  entitlement, partition or live buffer is presently healthy.


## Evidence index

- `datafetching/options_runtime.py:110`
- `datafetching/options_runtime.py:360`
- `datafetching/options_runtime.py:700`
- `datafetching/options_runtime.py:767`
- `datafetching/options_runtime.py:821`
- `datafetching/options_runtime.py:1018`
- `datafetching/options_runtime.py:1107`
- `datafetching/options_runtime.py:1242`
- `datafetching/databento_cold_start.py:665`
- `options/databento_live.py:33`
- `options/publication.py:92`
- `options/pending_capture.py:264`
- `tests/test_databento_opra_live.py:832`
- `tests/test_pricing_options_sequencing.py:737`
