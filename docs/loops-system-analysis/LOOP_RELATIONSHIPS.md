# Loop relationships

## Relationship conventions

The catalog includes only direct artifact/control exchanges or an explicit phase contract. Two loops reading the same datastore without consuming one another’s artifact is not a relationship. `D` = data, `C` = readiness/control, `T` = timing, `M` = model, `F` = fallback, and `H` = historical-only/asynchronous feedback.

## Direct relationship catalog

### R1 — CME/L2 → Directional Loop B

- **Status:** Confirmed.
- **Type:** D; optional-by-freshness but active in registered horizon profiles.
- **Exchange:** `pools/cme/features/cross-asset-context/databento/1h.parquet`; values `nq_return`, `es_return`, RTY-minus-ES, NQ-minus-ES, gold/crude returns, relative spread, book imbalance, quality/completeness and availability clocks.
- **Availability:** common completed 60-minute window; no future evidence; maximum 15-minute source staleness for context construction and horizon-specific feature freshness.
- **Consumer behavior:** joins by causal availability; stale/quality-failed values become unavailable. If no usable source exists, affected routes may fail, but no stale substitution is authorized.
- **Current-authority boundary:** the five-minute L2 pointer is not the artifact B reads. CME independently requires complete configured-symbol BBO/MBP freshness for that pointer, using an event boundary distinct from the local availability cutoff; older history recovery is bounded to at most one chunk and cannot delay or weaken the strict current lane. `datafetching/cme_runtime.py:157`, `datafetching/cme_history.py:288`, `datafetching/cme_history.py:532`
- **Producer evidence:** `datafetching/cme_cross_asset_context.py:24`, `datafetching/cme_cross_asset_context.py:174`, `datafetching/cme_cross_asset_context.py:277`
- **Consumer evidence:** `ml/rolling_materialization.py:796`, `ml/datasets/families.py:517`, `ml/feature_registry.py:499`

### R2 — Loop A → Active Pricing

- **Status:** Confirmed.
- **Type:** D + C.
- **Exchange:** exact all-symbol bar-readiness receipt/row checksums and each target close; completed Databento bars; prospective current-FRED rate context when available.
- **Availability:** exact eligible quarter-hour; Loop A polls Databento Historical schema availability and may refetch the exact minute for at most 420 seconds. Pricing waits at most 480 seconds, and both remain inside the independent 1,200-second causal window. A prematurely ended Databento response is classified as transient and retried by the shared bounded policy; identity, entitlement, and readiness-integrity failures still fail closed. `app/services/databento_retry.py:14`, `app/services/databento_retry.py:24`, `tests/test_databento_retry.py:6`
- **Consumer behavior:** bounded wait/retry; deadline causes a write-free skip and leaves prior Pricing authority unchanged.
- **Producer evidence:** `datafetching/orchestrate.py:292`, `datafetching/bar_readiness.py:120`, `datafetching/fred_vintages.py:600`
- **Consumer evidence:** `ml/option_pricing_runtime.py:1116`, `ml/option_pricing_runtime.py:1181`, `ml/option_pricing/rates.py:361`, `ml/option_pricing_runtime.py:1713`

### R3 — Loop A → Options Capture

- **Status:** Confirmed.
- **Type:** D + C.
- **Exchange:** exact bar-readiness decision clock/close for downstream pricing and Schwab normalization; latest complete Loop A time as the regime-data cutoff; persisted daily equity bars for realized volatility.
- **Availability:** prospective OPRA capture has independent provider/local clocks and can commit before Loop A readiness. Schwab publication requires exact target readiness; a missing receipt permits only checksum-sealed Schwab pending capture, and closed-market discovery can reconstruct exact clocks from persisted bars.
- **Consumer behavior:** OPRA commit, or Schwab commit/pending/reconcile/terminal expiry; it does not fabricate readiness.
- **Producer evidence:** `datafetching/bar_readiness.py:50`, `datafetching/loop_a_cycle.py:153`, `datafetching/orchestrate.py:326`
- **Consumer evidence:** `datafetching/options_runtime.py:266`, `datafetching/options_runtime.py:335`, `datafetching/options_runtime.py:454`, `options/features.py:335`

### R4 — Loop A → Directional Loop B

- **Status:** Confirmed.
- **Type:** D + C + shared single-reader/writer lock.
- **Exchange:** the current `.ducketz-loop-a-cycle.json` record and its `finished_at` causal cutoff; normalized equity bars/quotes, technicals, fundamentals, signals, current nonhistorical contexts, energy and SEC evidence. `.ducketz-loop-a-complete.json` is retained for independent readers such as Options, not substituted by B.
- **Availability:** Loop B acquires the shared datastore-cycle lock and requires the current Loop A cycle to be `COMPLETE`; `input_available_at` is that cycle’s finish time. A newer `WRITING` or `FAILED` record aborts the B attempt even if a prior latest-complete record remains.
- **Consumer behavior:** missing/noncomplete cycle fails cleanly; failed feature routes may be partial under the production default, while shared-authority corruption aborts the run.
- **Producer evidence:** `datafetching/loop_a_cycle.py:127`, `datafetching/loop_a_cycle.py:199`, `datafetching/orchestrate.py:389`
- **Consumer evidence:** `ml/prediction_runtime.py:209`, `ml/prediction_runtime.py:218`, `ml/rolling_materialization.py:272`

### R5 — Loop A → Strategy

- **Status:** Confirmed.
- **Type:** D; shared artifact without direct control coordination.
- **Exchange:** Schwab stock quote-liquidity Parquets (`symbol`, bid, ask, `available_at`, quality policy/status) used for entry/exit stock legs and candidate risk/quality.
- **Availability:** quotes are bounded by Strategy’s run/entry/exit cutoffs and validated for schema, natural key, and quality policy.
- **Consumer behavior:** missing stock quote returns `None`; option-only candidates can still be considered, while stock-dependent quality/risk evidence degrades or prevents affected constructions.
- **Producer evidence:** `datafetching/main.py:257`, `datafetching/parquet_store.py:121`
- **Consumer evidence:** `ml/strategy_selection/chain.py:151`, `ml/strategy_selection/chain.py:258`, `ml/strategy_selection/chain.py:391`

### R6 — Daily ALFRED → Active Pricing

- **Status:** Confirmed.
- **Type:** D + M.
- **Exchange:** point-in-time `FEDFUNDS` observations from `pools/macro/features/alfred-release-context/fred/*.parquet`; used as the required causal live risk-free input by fast target construction and by the owned residual-model worker.
- **Availability:** `fed_funds_available_at` must be strictly before the pricing decision/source boundary; rate is percentage points converted to decimal. Current-revised history is not eligible historical evidence.
- **Consumer behavior:** live construction requires the latest eligible ALFRED/FRED observation and disables both option-provider rate fallback and FMP-curve substitution. Offline/general materialization may retain the separately verified curve hierarchy; no invented rate is allowed. `ml/option_pricing/causal.py:264`, `ml/option_pricing/causal.py:450`, `ml/option_pricing/causal.py:468`
- **Producer evidence:** `datafetching/fred_vintages.py:344`, `datafetching/fred_vintages.py:364`, `datafetching/fred_alfred_readiness.py:185`
- **Consumer evidence:** `ml/option_pricing/rates.py:361`, `ml/option_pricing/rates.py:397`, `ml/option_pricing/rates.py:236`, `ml/option_pricing_loop_native_worker.py:54`

### R7 — Daily ALFRED → Directional Loop B

- **Status:** Confirmed.
- **Type:** D + C.
- **Exchange:** checksum-bound readiness authorization, immutable vintages, and derived macro context for `macro__fed_funds_level`, `macro__cpi_yoy`, `macro__unemployment_change`, and `macro__gdp_yoy`, each with its own clock.
- **Availability:** active only for daily and weekly contracts; minimum 95% causal coverage, zero lookahead violations, and verified importer lineage are required.
- **Consumer behavior:** missing/invalid readiness or corrupt evidence is a shared contract failure and aborts the whole prospective Loop B publication; stale individual values become missing by their own freshness.
- **Producer evidence:** `datafetching/fred_alfred_readiness.py:185`, `datafetching/fred_alfred_readiness.py:234`, `datafetching/fred_alfred_readiness.py:667`
- **Consumer evidence:** `ml/rolling_materialization.py:740`, `ml/rolling_materialization.py:757`, `ml/rolling_materialization.py:322`, `ml/datasets/families.py:785`

### R8 — Directional Loop B → Daily ALFRED

- **Status:** Confirmed; asynchronous historical feedback, not a same-cycle barrier.
- **Type:** M + H.
- **Exchange:** current authoritative `samples.parquet` decision grid for `1d`, `1w`, and weekly component horizons. It determines the earliest required ALFRED observation/realtime bounds and readiness coverage checks.
- **Availability:** a current verified Loop B publication with eligible decisions is required to derive backfill/incremental scope.
- **Consumer behavior:** missing horizons/decisions fails plan derivation. A new datastore must first publish an authoritative base/earlier-profile Loop B sample grid; the documented one-time ALFRED backfill can then derive and authorize v3 macro coverage before daily updates begin.
- **Producer evidence:** `ml/runtime_pipeline.py:695`, `ml/runtime_pipeline.py:794`
- **Consumer evidence:** `datafetching/fred_alfred_readiness.py:400`, `datafetching/fred_alfred_readiness.py:405`, `datafetching/fred_alfred_readiness.py:420`, `docs/datafetch-ml/current_start_command:21`

### R9 — Active Pricing → Options Capture

- **Status:** Confirmed.
- **Type:** C + F.
- **Exchange:** exact target-outcome receipt/pointer, terminal status, published time, prediction-row count, run path and receipt checksum embedded as `pricing_barrier` metadata in the Options receipt.
- **Availability:** authority must be verified and published before the Options request to receive prospective credit.
- **Consumer behavior:** blocks for at most 45 seconds in production; `MISSING`, `TIMED_OUT`, or a verified no-prediction/failure outcome does not prevent capture, but cannot receive causal Pricing-before-request credit.
- **Producer evidence:** `ml/option_pricing/target_outcome.py:93`, `ml/option_pricing_runtime.py:1306`
- **Consumer evidence:** `datafetching/pricing_barrier.py:77`, `datafetching/pricing_barrier.py:38`, `datafetching/options_runtime.py:250`

### R10 — Options Capture → Active Pricing

- **Status:** Confirmed.
- **Type:** D + M.
- **Exchange:** earlier committed canonical OPRA/explicit-Schwab-fallback chains for contract definitions, lagged IV, source BBO, residual samples and model fitting; the earliest eligible later-target chain reconciles a prior prediction into dollar/normalized error and interval coverage. Provider/evidence lane and `fallback_used` remain explicit.
- **Availability:** source quote/evidence must predate prediction. The owned OPRA callback yields cooperatively during replay, and a target read waits for the requested symbol's watermark to reach the target before selecting a final strictly pretarget BBO. A prospective outcome must come from a committed snapshot target at or after the prediction target, with exact contract quote and receipt after prediction availability; the later target and availability are retained, never backdated. Natural targets and receipt chains are checksum-verified. `options/databento_live.py:244`, `options/databento_live.py:280`
- **Consumer behavior:** missing/stale source produces explicit route failure/baseline absence; missing later outcome leaves evaluation pending. `OPRA_TARGET_WATERMARK_UNAVAILABLE` is bounded provider unavailability and may use separately labeled Schwab fallback; definition, identity, clock, or checksum failures do not. `options/databento_live.py:301`, `datafetching/options_runtime.py:454`
- **Producer evidence:** `options/publication.py:105`, `options/publication.py:402`, `options/snapshot.py:201`
- **Consumer evidence:** `ml/option_pricing/causal.py:107`, `ml/option_pricing_runtime.py:660`, `ml/option_pricing_runtime.py:675`

### R11 — Active Pricing → Directional Loop B

- **Status:** Confirmed.
- **Type:** D + F.
- **Exchange:** append-only compact `opx__` surfaces: coverage, residual, uncertainty, edge fractions, arbitrage rates, interval coverage and spread, plus provider/generation/availability provenance.
- **Availability:** every reachable Pricing generation is verified; Loop B selects the newest generation for a natural symbol/target whose first availability is no later than its causal cutoff. Freshness is 2 h/4 h/2 d/8 d by public horizon.
- **Consumer behavior:** unavailable/stale/uncovered data becomes audited null and triggers the non-Pricing baseline feature set; corrupt authority aborts publication.
- **Producer evidence:** `ml/option_pricing_runtime.py:707`, `ml/option_pricing/publication.py:36`, `ml/option_pricing/consumers.py:306`
- **Consumer evidence:** `ml/rolling_materialization.py:663`, `ml/rolling_materialization.py:677`, `ml/runtime_pipeline.py:432`

### R12 — Options Capture → Directional Loop B

- **Status:** Confirmed.
- **Type:** D.
- **Exchange:** committed `opt__` option-quality surface values and quality/timing clocks.
- **Availability:** only receipt-committed surfaces at or before the Loop B input cutoff; family-specific freshness and `surface_quality_pass` apply.
- **Consumer behavior:** exact committed history is preferred; legacy feature files are a compatibility fallback only when no committed snapshots exist. Missing/invalid family data can fail affected routes.
- **Producer evidence:** `options/features.py:217`, `options/features.py:285`, `options/publication.py:402`
- **Consumer evidence:** `ml/rolling_materialization.py:614`, `ml/rolling_materialization.py:626`, `ml/datasets/families.py:263`

### R13 — Directional Loop B → Strategy

- **Status:** Confirmed.
- **Type:** D + M + C.
- **Exchange:** authoritative Loop B source record/receipt, redacted samples, LIVE calibrated directional probabilities, target windows, feature context and causal input cutoff.
- **Availability:** Strategy reads one verified current pointer, captures its complete current record, and binds that exact record and its manifest/receipt checksums into the Strategy manifest and publication receipt; predictions must match exactly one sample and precede entry. `ml/strategy_runtime.py:63`, `ml/strategy_runtime.py:235`, `ml/strategy_publication.py:41`
- **Consumer behavior:** no valid current Loop B run, samples, or predictions fails the Strategy cycle. The unchanged-work fingerprint requires exact equality with the current Loop B pointer plus the pricing mode and both prospective OPRA and Schwab per-symbol snapshot heads. A valid dynamic weekly bundle contributes only its actually published prefix; Strategy does not synthesize calendar-inapplicable component predictions. `ml/strategy_runtime.py:429`, `ml/runtime_pipeline.py:4005`
- **Producer evidence:** `ml/runtime_pipeline.py:704`, `ml/runtime_pipeline.py:876`
- **Consumer evidence:** `ml/strategy_runtime.py:74`, `ml/strategy_runtime.py:81`, `ml/strategy_runtime.py:125`, `ml/strategy_runtime.py:414`

### R14 — Options Capture → Strategy

- **Status:** Confirmed.
- **Type:** D + M.
- **Exchange:** OPRA-first provider-neutral entry/exit chain history, point-in-time definitions, contract BBOs, option-quality evidence, and observed option outcomes. Verified Schwab snapshot history remains the explicit fallback when eligible OPRA definitions/BBOs are unavailable; Schwab stock quote-liquidity remains available for stock legs.
- **Availability:** entry must be known after information availability and before target start; exit is the earliest receipt after target end within a horizon-specific delay and before any lockbox boundary.
- **Consumer behavior:** missing chain/entry/exit evidence is audited and the affected candidate/outcome is skipped rather than synthesized.
- **Producer evidence:** `options/publication.py:406`, `options/snapshot.py:499`
- **Consumer evidence:** `ml/strategy_selection/chain.py:97`, `ml/strategy_selection/runtime.py:240`, `ml/strategy_selection/runtime.py:395`

### R15 — Active Pricing → Strategy

- **Status:** Confirmed.
- **Type:** D + M + F.
- **Exchange:** exact-contract baseline predictions, one-to-one residual sidecars and verified generation history; `BSGP_SHADOW_READY` becomes Strategy source `BSGP`, while complete residual fallback becomes `BLACK_SCHOLES`. Per leg, Strategy receives fair-value edge, conservative edge, uncertainty, favorable probability, model age and residual shrinkage.
- **Availability:** Pricing evidence must be receipt-verified and available before the candidate probability; live scoring disallows offline replay.
- **Consumer behavior:** full active leg coverage admits fitted Strategy scoring; missing/delayed coverage keeps a separately typed non-probabilistic scenario-coverage heuristic with all model-probability fields null.
- **Producer evidence:** `ml/option_pricing/target_outcome.py:93`, `ml/option_pricing/publication.py:83`, `ml/option_pricing/strategy_shadow.py:263`, `ml/option_pricing/strategy_shadow.py:298`
- **Consumer evidence:** `ml/strategy_selection/runtime.py:93`, `ml/strategy_selection/runtime.py:288`, `ml/strategy_selection/runtime.py:310`, `ml/strategy_selection/runtime.py:637`

### R16 — Directional Loop B → Options Capture (phase only)

- **Status:** Documented only as a relative timing relationship; no direct data/control dependency.
- **Type:** T.
- **Exchange:** none. Startup says Options starts after Loop B’s +5 information clock; code schedules B at +5 and Options at +6, but Options never reads the Loop B pointer.
- **Consumer behavior:** none; Options proceeds independently based on Loop A, Pricing barrier, pending state, and provider evidence.
- **Evidence:** `docs/datafetch-ml/current_start_command:99`, `docs/datafetch-ml/current_start_command:160`, `docs/datafetch-ml/current_start_command:188`, `datafetching/options_runtime.py:700`, `datafetching/options_runtime.py:821`

### R17 — Strategy-profit training → Strategy

- **Status:** Confirmed; asynchronous model authority, not a same-cycle barrier.
- **Type:** M.
- **Exchange:** receipt-gated fitted-model artifacts and conservative execution
  evidence for the exact 1d/1w Strategy scope; compatible weekly components
  reuse the one-session model.
- **Availability:** the daily owner runs at 22:00 UTC. A later Strategy cycle may
  consume only a checksum-valid compatible authority.
- **Consumer behavior:** missing, immature, or incompatible authority leaves
  candidates on explicit nonprobabilistic Scenario Coverage; it never grants an
  order path.
- **Producer evidence:** `ml/strategy_profit_training_runtime.py`,
  `ml/strategy_profit_training.py`
- **Consumer evidence:** `ml/strategy_runtime.py`,
  `ml/strategy_selection/model.py`

## Owned-worker relationships

**Confirmed:** Active Pricing launches the one-shot loop-native worker after fast target publication without blocking Options. The worker reads ALFRED/FRED rates and verified local OPRA-first/Schwab-fallback history, publishes a local materialization/model/status under its own lock, and makes no provider request. A later Pricing cycle may load that prior model; Strategy may also read verified replay evidence, but live fitted scoring does not relabel offline replay as prospective. `ml/option_pricing_runtime.py`, `ml/option_pricing_loop_native_worker.py`, `ml/option_pricing/strategy_shadow.py`

## OPRA boundaries that are not loop-to-loop relationships

- **Confirmed prospective boundary:** `DatabentoOpraLiveAdapter` implements the injected `OptionMarketDataAdapter` protocol inside Options Capture. It owns one scoped, bounded/reconnecting `OPRA.PILLAR` definitions + `cbbo-1s` transport and no separate supervisor. Dense callback replay yields cooperatively so the publication thread can run; each target read requires a per-symbol watermark. The default production CLI constructs it before entering the recurring loop; missing configuration/startup fails clearly. Only bounded `OptionProviderUnavailable`, including target-watermark unavailability, may use labeled Schwab fallback; identity/integrity failures fail closed. `options/databento_live.py:34`, `options/databento_live.py:244`, `options/databento_live.py:280`, `datafetching/options_runtime.py:430`, `datafetching/options_runtime.py:454`
- **Confirmed historical boundary:** `datafetching.options_history` is the normal one-time prediction-focused per-parent bootstrap. It prioritizes 100 days of definitions, 20 days of `cbbo-1m`, 1 day of `cbbo-1s`, and the bounded OHLCV windows; research-only `cmbp-1` remains explicitly selectable but default-deferred. The optional `datafetching.databento_cold_start` command can populate the same canonical OPRA partitions alongside CME and US-equity provider archives, where historical `mbp-1` is also default-deferred in favor of prediction-consumed `mbp-10`. The `XNAS.ITCH` equity archive is cold provenance while Loop A uses current `EQUS.MINI` operational continuation; different provider dataset identities are deliberately not merged. CME has a parallel exact-spec cursor/context bridge. After each verified OPRA scope the command publishes a v5 symbol/schema cursor containing the exact requested start, completion boundary, lookback policy, and bootstrap manifest. Options Capture runs at most one daily catch-up for valid cursors; a missing/invalid cursor is bootstrap-required. `ml.option_pricing_opra` is the separate full-universe/custom-scope administrative synchronizer. Active Pricing and offline Strategy workflows consume verified local OPRA partitions without fetching history; live Strategy entry/Pricing attachment explicitly forbid offline replay. `datafetching/options_history.py`, `datafetching/databento_cold_start.py`, `datafetching/databento_archive.py:213`, `datafetching/equity_dataset_migration.py`, `datafetching/databento_archive.py:539`, `datafetching/options_runtime.py`, `ml/option_pricing_opra_replay.py`, `ml/strategy_selection/runtime.py:167`
- **Confirmed cold-start ownership boundary:** the cold-start command holds `.ducketz-databento-cold-start.lock` and, for OPRA only, the canonical history `state/sync.lock`. It never takes the CME, Loop A, or Options snapshot-writer locks and never publishes Loop A readiness, option snapshots, ML/Strategy pointers, or model authority. Its readable CME and US-equity scope records are not live-loop cursors or pointers; verified bridge code may use their evidence to seed operational continuation under the receiving owner's contracts. `datafetching/databento_cold_start.py`, `datafetching/databento_archive.py:213`, `datafetching/databento_archive.py:539`
- **Confirmed conditional equity archive bridge and current separation:** `materialize_equity_archive_baseline` can materialize only when archive and operational dataset identities match. The recurring wrapper currently sees cold `XNAS.ITCH` versus operational `EQUS.MINI`, logs provenance, and returns without merging rows. Native EQUS fetch then overlaps its own operational timestamps. `datafetching/databento_archive.py:213`, `datafetching/databento_fetch.py:544`, `datafetching/equity_dataset_migration.py:537`
- **Confirmed CME archive bridge:** if a runtime cursor is absent, `cme_archive_cursor_for_spec` verifies a matching archive scope and supplies the first continuation boundary. Cross-asset materialization fingerprints archive inventories, combines archive and ongoing runtime rows, appends unseen historical/current common windows, and checksum-binds lineage. Separately, the runtime collects exact short current BBO/MBP windows, requires every configured stream fresh, and checkpoints at most one older recovery chunk. The bridge does not grant archive code a CME lock, live cursor, L2 pointer, or publication authority. `datafetching/databento_archive.py:539`, `datafetching/cme_runtime.py:157`, `datafetching/cme_history.py:532`, `datafetching/cme_cross_asset_context.py:250`
- **Confirmed model relationship, not process coordination:** the resulting residual architecture implements the reference `f(x)=BS(x)+delta(x)` pattern with six inputs, but uses a bounded Nyström/Bayesian-ridge posterior in production and retains an exact-GP SPY path only for research. `docs/edu/BLACK-SCHOLES-OP.md:327`, `docs/edu/BLACK-SCHOLES-OP.md:441`, `ml/option_pricing/model.py:68`, `ml/option_pricing/research_benchmark.py:34`

## Dependency matrix

Rows are producers; columns are consumers. Empty cells mean no direct exchange. `T(doc)` is phase-only and does not create causal contribution.

| Producer ↓ / Consumer → | CME/L2 | Loop A | Daily ALFRED | Active Pricing | Options Capture | Directional B | Strategy |
|---|---|---|---|---|---|---|---|
| CME/L2 | — |  |  |  |  | D: `cme__` context |  |
| Loop A |  | — |  | D+C: bar receipt, close, rates | D+C: readiness, bars/regime | D+C: complete cycle and feature data | D: stock BBO |
| Daily ALFRED |  |  | — | D+M: causal FEDFUNDS |  | D+C: vintage macros/readiness |  |
| Active Pricing |  |  |  | — | C+F: target barrier | D+F: `opx__` surfaces | D+M+F: exact leg pricing |
| Options Capture |  |  |  | D+M: lagged chains/outcomes | — | D: `opt__` surfaces | D+M: entry/exit chains/outcomes |
| Directional B |  |  | M+H: decision grid |  | T(doc): +5 before +6 | — | D+M+C: samples/probabilities |
| Strategy |  |  |  |  |  |  | — |

## Fan-in, fan-out, and critical path

- **Confirmed fan-in — Active Pricing:** Loop A exact readiness, Daily ALFRED/current-FRED rates, earlier Options snapshots, and an optional prior owned-worker model. `ml/option_pricing_runtime.py:309`, `ml/option_pricing_runtime.py:1116`, `ml/option_pricing_runtime.py:1181`
- **Confirmed fan-in — Directional Loop B:** complete Loop A authority plus Loop A feature files, CME context, ALFRED readiness/vintages, Options surfaces, and Pricing compact history. `ml/prediction_runtime.py:209`, `ml/rolling_materialization.py:614`, `ml/rolling_materialization.py:663`, `ml/rolling_materialization.py:740`, `ml/rolling_materialization.py:796`
- **Confirmed fan-in — Strategy:** Loop B publication, immutable prospective provider-neutral receipts with OPRA priority per target, stock BBO history, and the Pricing catalog are live inputs. Canonical OPRA replay/cache is an eligible offline history/outcome input only; recurring live entry and live Pricing attachment reject it. The +10 supervisor observes both prospective provider heads. `ml/strategy_runtime.py`, `ml/strategy_selection/chain.py:111`, `ml/strategy_selection/runtime.py:167`
- **Confirmed fan-out — Loop A:** exact readiness to Pricing/Options, complete-cycle/features to B, and stock BBO to Strategy. `datafetching/orchestrate.py:292`, `datafetching/loop_a_cycle.py:127`, `ml/strategy_selection/chain.py:151`
- **Confirmed fan-out — Options Capture:** supplies future Pricing samples/evaluations, `opt__` features to B, and exact strategy chains/outcomes. `ml/option_pricing_runtime.py:660`, `ml/rolling_materialization.py:614`, `ml/strategy_selection/runtime.py:109`
- **Confirmed fan-out — Active Pricing:** target barrier to Options, compact `opx__` features to B, and leg pricing to Strategy. `datafetching/pricing_barrier.py:100`, `ml/option_pricing/consumers.py:306`, `ml/option_pricing/strategy_shadow.py:74`

### Critical-path interpretation

- **Directional horizon publication:** Loop A complete authority is mandatory. Valid ALFRED authority is mandatory for the active v3 daily/weekly profile and invalid shared ALFRED/Pricing authority aborts the materialization. Missing valid Pricing rows alone is noncritical because the baseline feature set is explicit. CME and Options can lag while existing evidence remains within freshness; absent/invalid required family sources can fail affected routes. `ml/prediction_runtime.py:209`, `ml/rolling_materialization.py:322`, `ml/runtime_pipeline.py:455`
- **Option-pricing target publication:** exact Loop A readiness and valid causal contract/rate/option inputs are critical for each target/route. The fitted residual model is not critical because the Black–Scholes point estimate and explicit sidecar fallback remain supported. Ready residual values can still affect Strategy through the separately verified sidecar. `ml/option_pricing_runtime.py:1128`, `ml/option_pricing_runtime.py:1220`, `ml/option_pricing/strategy_shadow.py:298`
- **Options capture:** Pricing success is not critical to capture. Target-scoped OPRA selection/commit uses its own point-in-time clocks and does not wait for Loop A; exact Loop A readiness remains mandatory for downstream Pricing and Schwab normalization. If OPRA is transiently unavailable while readiness is delayed, the single fallback Schwab request is durably claimed and quarantined pending reconciliation. `datafetching/pricing_barrier.py:124`, `datafetching/options_runtime.py:360`, `datafetching/options_runtime.py:369`, `datafetching/options_runtime.py:452`, `options/pending_capture.py:118`
- **Options-strategy predictions:** a verified Loop B run and an eligible entry option-chain receipt are critical. Active Pricing plus a fitted and calibrated Strategy model authorizes probability scoring; otherwise Strategy exposes only separately typed Scenario Coverage. `ml/strategy_runtime.py`, `ml/strategy_selection/runtime.py`

## Explicit non-relationships

- **Confirmed:** CME/L2 and Loop A both use Databento but do not coordinate or consume each other; external CME ownership is deliberately isolated. `datafetching/orchestrate.py:90`, `tests/test_independent_loop_isolation.py:17`
- **Confirmed:** Options does not consume Loop B despite the +5/+6 phase ordering. Its implemented upstreams are Loop A readiness, Pricing barrier, pending state, and providers. `datafetching/options_runtime.py:250`, `datafetching/options_runtime.py:266`
- **Confirmed:** Strategy is not an inline stage of Loop B; Loop B’s manifest declares independent strategy authority, and tests assert directional publication does not wait for Strategy. `ml/runtime_pipeline.py:838`, `tests/test_ml_runtime_pipeline.py:454`
- **Confirmed:** Loop C, the exact Options Strategy paper ledger, stock-trader
  checkpoints, and weekly review are bounded Codex Scheduled invocations, not
  persistent ninth owners. Their scoped broker/mutation boundaries do not flow
  through the loop relationship matrix.

## Operational monitoring relationship

The monitor is not a ninth production owner and does not join the dataflow
matrix. Hourly mode independently proves exact process pairs, worker locks,
active logs, publications, lineage, UI contracts, and storage. Daily mode adds
all route/output evaluations. Weekly mode adds a source-grounded comparison of
the last two completed XNYS session weeks only when contracts match and both
periods have at least 30 independent LIVE evaluations; otherwise it reports
insufficient or incompatible evidence. The guardian may act only on one
allowlisted unambiguous liveness fault and leaves all data/model/lineage
findings report-only. `ml/system_monitor.py:164`,
`ml/system_monitor.py:1375`, `ml/system_guardian.py:237`

**Observed 2026-08-19 22:45:36 UTC:** all seven process/lock relationships,
publication authorities, and both UI contracts passed. Strategy authority
`ml/strategy-runs/20260819T224000.073641Z` was checksum-bound to the exact
current Loop B record `ml/runs/20260819T223552.337574Z`; the former lineage
warning was no longer present. A 22:59:29 UTC read-only follow-up remained
`HEALTHY` after both owners advanced again with exact current lineage. The only
`INFO` was the valid closed-market absence of an Active Pricing target, not a
relationship, process, or publication fault.
