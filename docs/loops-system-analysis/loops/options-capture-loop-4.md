# Options Capture / logical Loop 4

## Identity

- Canonical name: Options Capture runtime
- Logical aliases or numbering: logical Loop 4; startup owner 5
- Runtime entry point: `python -m datafetching.options_runtime`
- Owning package: `datafetching` with `options` publication/normalization contracts
- Classification: Independent production loop
- Scheduling mechanism: recurring quarter-hour supervisor with bounded internal barrier polling and pending-capture reconciliation
- Cadence and phase: every 15 minutes at UTC phase +6 minutes; 45-second default Pricing-barrier wait
- Lock or single-writer mechanism: `.ducketz-options-writer.lock`, shared by all committed option-snapshot publications
- Primary code evidence: **Confirmed.** `datafetching/options_runtime.py:577`, `datafetching/options_runtime.py:583`, `datafetching/options_runtime.py:587`, `datafetching/options_runtime.py:643`, `datafetching/options_runtime.py:648`

## Purpose

**Confirmed:** Options Capture owns acquisition and immutable publication of provider-neutral option-chain evidence at the calendar target. It attempts canonical Databento OPRA L1 evidence when an adapter is configured, labels and falls back to Schwab on failure, computes a compact option-quality/features row, and publishes an atomic per-provider/per-symbol snapshot. If exact Loop A readiness is not yet available, it may make the one provider request inside the causal window, seal the response under a non-production pending authority, and reconcile it only after exact readiness is provable. `datafetching/options_runtime.py:349`, `datafetching/options_runtime.py:362`, `datafetching/options_runtime.py:404`, `options/pending_capture.py:264`

**Confirmed OPRA scope:** this loop implements the prospective `OPRA.PILLAR` `cbbo-1s` ingestion boundary. The separate `ml.option_pricing_opra` command implements authorized historical `cbbo-1m` acquisition for model evidence and is maintenance, not part of this supervisor. Tests exercise the prospective adapter with no Schwab call and prove a repeated natural target makes no second OPRA request. `options/providers.py:25`, `ml/option_pricing/opra.py:33`, `tests/test_option_publication.py:403`, `tests/test_option_publication.py:470`

**Confirmed non-ownership:** it does not declare bars ready, value contracts, fit directional or strategy models, or make orders. A Pricing target result is sequencing evidence, not permission to fabricate or alter option data. `datafetching/pricing_barrier.py:38`, `options/publication.py:105`

## Inputs

| Input or dataset | Producer/source | Physical path or interface | Key fields and semantic values | Clock/freshness/causality rules | Required or optional | Evidence |
|---|---|---|---|---|---|---|
| Target decision and exact bar readiness | Loop A | target-scoped readiness receipt or exact persisted completed bars | exact quarter-hour, per-symbol decision bar/close/provider/timeframe, decision timestamp and source file | Required production commit must match the calendar target; readiness clock cannot be future; discovery mode reconstructs exact clocks without claiming a receipt | Required for commit; not required to create pending evidence | **Confirmed.** `datafetching/options_runtime.py:117`, `datafetching/options_runtime.py:266`, `datafetching/options_runtime.py:289` |
| Latest complete Loop A cutoff and daily bars | Loop A | complete-cycle pointer plus normalized price history | `finished_at` bounds regime evidence; adjusted-close history supplies causal 20-day realized volatility and split state | Regime inputs must be available no later than request; no post-request Loop A evidence | Required for committed feature lineage; feature values may be missing with status | **Confirmed.** `datafetching/options_runtime.py:335`, `datafetching/options_runtime.py:461`, `options/features.py:288` |
| Target Pricing outcome/barrier | Active Pricing | verified target outcome/receipt | barrier `VERIFIED`, `MISSING`, or `TIMED_OUT`; Pricing terminal status, prediction row count, publish/observe/request clocks, receipt checksum; `prospective_credit_allowed` | Wait at most configured 45 seconds; credit only when Pricing authority and observation precede the request and predictions exist | Optional for capture; mandatory only for prospective Pricing-before-capture credit | **Confirmed.** `datafetching/pricing_barrier.py:18`, `datafetching/pricing_barrier.py:38`, `datafetching/pricing_barrier.py:77` |
| Canonical OPRA snapshot | Databento adapter, when injected/configured | `OptionMarketDataAdapter` carrying `OPRA.PILLAR` `cbbo-1s` evidence | contract definitions and final strictly pretarget BBO, bid/ask size/trade, contract/call-put/strike/expiration/multiplier, publisher and receipt clock | Adapter identity and returned provider/dataset/schema/symbol/target are validated; definitions effective by target; quotes strictly before target and noncrossed | Optional preferred provider lane; implementation confirmed, concrete live transport is a rollout dependency | **Confirmed.** `options/providers.py:25`, `datafetching/options_runtime.py:365`, `datafetching/options_runtime.py:370`, `options/snapshot.py:122`, `options/snapshot.py:183` |
| Schwab option chain | Schwab session | `get_option_chain_snapshot` | contract symbol, expiration, DTE, strike, bid/ask/mark, volume/OI, IV, Greeks, theoretical/intrinsic values and underlying quote | One request per unclaimed symbol/target; quote cutoff is request start; response time becomes first availability; persistent bounded provider retry | Explicit fallback and the broker lane instantiated by the numbered CLI | **Confirmed.** `datafetching/options_runtime.py:413`, `datafetching/options_runtime.py:424`, `datafetching/options_runtime.py:663`, `options/snapshot.py:495` |
| Earlier committed/pending state | This loop | snapshot pointers/receipts and `options/pending-captures/schwab/<target>/<symbol>/` | natural target claims, request/response clocks, raw payload checksum, `REQUEST_STARTED`, `PENDING_READINESS`, reconciled/expired/failed state | Existing committed target skips provider; pending claim is durable before its sole request; request must be target through target +1,200 seconds | Optional on bootstrap; authoritative when present | **Confirmed.** `datafetching/options_runtime.py:162`, `options/pending_capture.py:100`, `options/pending_capture.py:118`, `options/pending_capture.py:147` |

## Processing and decisions

1. **Confirmed:** reconcile prior pending captures, derive the only eligible target, and skip symbols already committed or claimed. Closed-market discovery may refresh the latest eligible target but does not relabel it as a new calendar target. `datafetching/options_runtime.py:108`, `datafetching/options_runtime.py:117`, `datafetching/options_runtime.py:162`
2. **Confirmed:** poll the target Pricing authority for a bounded interval. The internal `while` in the barrier is a wait loop, not a runtime owner; timeout returns explicit metadata and capture continues. `datafetching/pricing_barrier.py:77`, `datafetching/pricing_barrier.py:98`, `datafetching/pricing_barrier.py:124`
3. **Confirmed:** read exact Loop A readiness. If absent, derive exact historical decision clocks only in supported discovery/compatibility paths; otherwise durably claim a pending target before requesting data. `datafetching/options_runtime.py:268`, `datafetching/options_runtime.py:285`, `datafetching/options_runtime.py:349`
4. **Confirmed:** when an adapter is injected, try OPRA for readiness-proven targets, validate its identity and normalize strictly pretarget quotes. Any adapter error is explicit and triggers a separately labeled Schwab request; OPRA is never fabricated. Without an adapter the same supervisor starts directly at the Schwab lane. `datafetching/options_runtime.py:362`, `datafetching/options_runtime.py:370`, `datafetching/options_runtime.py:404`, `datafetching/options_runtime.py:413`
5. **Confirmed:** for Schwab, fetch once, normalize contract/quote fields, compute option-quality features and causal realized-volatility context, then either seal the payload pending readiness or publish it under the exact decision clock. `datafetching/options_runtime.py:424`, `datafetching/options_runtime.py:437`, `datafetching/options_runtime.py:454`
6. **Confirmed:** publication validates coherent raw/contracts/feature keys, writes them to a staging directory, checksums each output, commits the directory and atomically advances the provider pointer. Identical retries reuse; divergent retries fail closed. `options/publication.py:120`, `options/publication.py:139`, `options/publication.py:163`, `options/publication.py:187`
7. **Confirmed:** reconciliation verifies pending request/capture, exact later readiness and causal expiry before invoking the same commit path. Pending response polling/reconciliation is owned internal state, not another production loop. `options/pending_capture.py:204`, `options/pending_capture.py:264`, `options/pending_capture.py:375`

## Outputs

| Output | Consumer(s) | Physical path or interface | Key output values and meanings | Publication/authority rules | Evidence |
|---|---|---|---|---|---|
| Provider-neutral immutable snapshot | Active Pricing; Directional Loop B; Strategy | `stocks/<symbol>/options/snapshots/<provider>/<target>/` | `raw.parquet`, `contracts.parquet`, `option-quality.parquet`; exact target, first availability, provider/dataset, contracts, bid/ask/mid, IV/Greeks/liquidity, quality features, Pricing-barrier proof | Natural key `(provider, symbol, target_snapshot_for)`; coherent nonempty frames; receipt/checksums; identical retry only; writer lock | **Confirmed.** `options/publication.py:23`, `options/publication.py:30`, `options/publication.py:92`, `options/publication.py:187` |
| Per-provider current pointer | Pricing, B, Strategy/UI readers | `stocks/<symbol>/options/latest/<provider>.json` | provider/dataset, target, availability, snapshot path and receipt identity | Atomic and monotone in target/availability; older/equal authority does not displace current | **Confirmed.** `options/publication.py:75`, `options/publication.py:814`, `options/publication.py:842` |
| Compact option feature row | Directional Loop B and indirectly Strategy | snapshot `option-quality.parquet` | ATM implied move/IV, realized-vol comparison, term/skew/smile, OI/volume ratios, parity, spread/staleness/coverage and quality pass/status | Derived only from causal rows; quality pass requires coverage, freshness, noncrossed/nonlocked positive quotes and no intrinsic violation | **Confirmed.** `options/features.py:186`, `options/features.py:199`, `options/features.py:214`, `options/features.py:285` |
| Pending capture authority | This loop only until reconciliation | `options/pending-captures/schwab/<target>/<symbol>/` | exact scope and target, request/response clocks, sealed raw payload/checksum, Pricing barrier metadata, `PENDING_READINESS`/reconciled/expired/failed states | Not a production snapshot and cannot authorize actions; immutable response; commit only after exact readiness; terminal expiry prevents late relabeling | **Confirmed.** `options/pending_capture.py:100`, `options/pending_capture.py:179`, `options/pending_capture.py:264` |
| Cycle/failure result | operator/error authority | console result and datastore failure records | published/failed/skipped; OPRA/Schwab calls/fallbacks; barrier and pending/reconciled/expired counts | Per-symbol failure isolation; successful symbols remain committed | **Confirmed.** `datafetching/options_runtime.py:554`, `datafetching/options_runtime.py:484` |

## Direct loop relationships

### Upstream

- **Confirmed:** Loop A provides exact commit clocks, readiness and regime/realized-volatility evidence. `datafetching/options_runtime.py:270`, `datafetching/options_runtime.py:335`
- **Confirmed:** Pricing supplies the optional bounded barrier proof; capture does not block indefinitely or require a successful prediction. `datafetching/options_runtime.py:250`, `datafetching/pricing_barrier.py:52`

### Downstream

- **Confirmed:** Pricing reads earlier chains as prediction inputs and later chains as outcomes. `ml/option_pricing_runtime.py:466`, `ml/option_pricing/consumers.py:372`
- **Confirmed:** Loop B joins compact `opt__` option-quality features by point-in-time availability. `ml/rolling_materialization.py:614`
- **Confirmed:** Strategy selects exact entry and exit chain receipts and uses contract rows to construct and evaluate candidates. `ml/strategy_selection/runtime.py:240`, `ml/strategy_selection/runtime.py:383`, `ml/strategy_selection/runtime.py:396`

### Timing and control relationships

**Confirmed:** intended phase is +6 minutes, after Pricing +1 and B +5. The B relationship is timing only in the startup/flowchart; Options consumes no B artifact. Pricing’s 45-second barrier is optional for capture but its proof determines whether an option snapshot can receive prospective Pricing-before-request credit. `docs/datafetch-ml/current_start_command:88`, `docs/datafetch-ml/current_start_command:112`, `datafetching/pricing_barrier.py:52`

## Prediction contribution

| Prediction family | Contribution | Explanation and exact causal chain |
|---|---|---|
| Directional horizon predictions | Indirect | committed chain → option-quality `opt__` feature row → Loop B materialization → calibrated horizon probability. `options/features.py:214`, `ml/rolling_materialization.py:614`, `ml/runtime_pipeline.py:493` |
| Option-pricing predictions | Indirect | earlier committed chain + later observed chain → Pricing sample/prediction input and realized evaluation/model evidence. `ml/option_pricing/consumers.py:372`, `ml/option_pricing_runtime.py:466` |
| Options-strategy predictions | Indirect | exact entry/exit chains → construct candidates and historical outcomes → fit/score profitable-outcome probability and rank. `ml/strategy_selection/runtime.py:240`, `ml/strategy_selection/runtime.py:351`, `ml/strategy_selection/runtime.py:311` |

**Roll-up classification: Both.** It has direct evidence paths into directional predictions and both option-related prediction types, while publishing none of those final authorities itself.

## Failure and degradation behavior

- **Confirmed:** a missing Pricing outcome times out explicitly; the request proceeds with no prospective Pricing credit, so Pricing failure does not by itself prevent capture. `datafetching/pricing_barrier.py:121`, `datafetching/pricing_barrier.py:181`
- **Confirmed:** missing readiness quarantines a Schwab response as pending; reconciliation commits only if exact readiness arrives within the causal contract, otherwise records expiry/failure. `datafetching/options_runtime.py:534`, `options/pending_capture.py:291`, `options/pending_capture.py:375`
- **Confirmed:** OPRA failure falls back to labeled Schwab. Provider or per-symbol normalization/commit failure is isolated and recorded; other symbols continue. `datafetching/options_runtime.py:404`, `datafetching/options_runtime.py:484`
- **Confirmed:** stale/future/crossed/locked/nonpositive or incomplete quotes fail quality or are excluded; quality state is retained instead of silently substituting a good status. `options/features.py:111`, `options/features.py:199`
- **Confirmed:** a conflicting natural-key retry or corrupt pointer/receipt fails closed. `options/publication.py:139`, `options/publication.py:827`

## Accuracy and efficiency relevance

- Leakage/target integrity: exact bar decision clock, pretarget OPRA filtering, request/response/receipt clocks, pending quarantine and immutable natural key. `options/snapshot.py:183`, `options/pending_capture.py:147`, `options/publication.py:107`
- Feature/prediction quality: explicit provider identity/fallback, contract definition timing, spread/coverage/staleness gates, realized-vs-implied/term/skew/parity families. `options/snapshot.py:201`, `options/features.py:199`, `options/features.py:238`
- Critical-path/provider volume: bounded 45-second barrier; committed/claimed target skip; one durable pending claim precedes its sole provider call; per-symbol isolation. `datafetching/options_runtime.py:162`, `options/pending_capture.py:129`
- Storage I/O: three immutable files and one small pointer per provider/symbol/target; identical retries reuse rather than rewrite. `options/publication.py:23`, `options/publication.py:145`

## Conflicts, gaps, and uncertainty

- **Confirmed current boundary, not an architectural conflict:** the CLI help retains a Schwab-oriented description because that numbered command instantiates the broker lane; `run_options_cycle` and the publication contract are provider-neutral and implement optional OPRA-first capture. Repository documentation explicitly calls the concrete live adapter/credentials a rollout dependency. `datafetching/options_runtime.py:579`, `datafetching/options_runtime.py:663`, `options/README.md:29`
- **Confirmed aliasing:** “logical Loop 4” is the functional alias; startup owner 5 includes Daily ALFRED as owner 3. The obsolete SVG is not used as current deployment evidence. `docs/datafetch-ml/current_start_command:61`, `docs/datafetch-ml/current_start_command:95`
- **Documented only:** the startup phase places B one minute before Options; no direct B → Options data/control API was found.
- **Unknown operational state:** because secrets and datastore contents were intentionally excluded, static analysis cannot establish whether the concrete prospective OPRA adapter is enabled, whether historical OPRA acquisition has executed, whether captures are populated, or the live OPRA/Schwab share. These are deployment/population unknowns, not missing ingestion implementation.
- **Confidence:** High for both OPRA and Schwab code paths, ownership, publication, barriers, pending state, and consumers; Medium only for deployed provider mix.

## Evidence index

- `datafetching/options_runtime.py:81`
- `datafetching/options_runtime.py:250`
- `datafetching/options_runtime.py:349`
- `datafetching/options_runtime.py:454`
- `datafetching/pricing_barrier.py:77`
- `options/publication.py:92`
- `options/pending_capture.py:118`
- `options/features.py:186`
- `options/providers.py:25`
- `tests/test_option_publication.py:403`
- `tests/test_option_pricing_opra.py:313`
- `tests/test_pricing_options_sequencing.py:327`
- `tests/test_pricing_options_sequencing.py:1323`
