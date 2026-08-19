# Prediction-contribution matrix

## Classification rule

**Confirmed convention:** `Direct` is reserved for the loop that publishes the authoritative prediction artifact for that family. `Indirect` requires an implemented data or control chain into that artifact; phase proximity alone does not count. `None` means no such path was found. Roll-up `Both` means at least one directional-horizon path and at least one option-pricing or options-strategy path; `Options` means option-family only. This convention describes causal contribution, not proven empirical lift.

## Matrix

| Loop | Directional horizon contribution | Option-pricing contribution | Options-strategy contribution | Roll-up | Exact outputs that create the contribution | Immediate downstream consumer | Evidence | Confidence |
|---|---|---|---|---|---|---|---|---|
| CME/L2 runtime | Indirect | None | Indirect | Both | verified archive-plus-live hourly causal cross-asset context: futures returns/breadth, relative spread, book imbalance, quality/availability | Directional Loop B; Strategy only through B | `datafetching/databento_archive.py:539`, `datafetching/cme_cross_asset_context.py:250`, `ml/rolling_materialization.py:782` | High for path; Unknown empirical lift |
| Loop A | Indirect | Indirect | Indirect | Both | current `EQUS.MINI` overlapping operational continuation; separate cold `XNAS.ITCH` provenance; exact bar-readiness/close; complete-cycle cutoff and feature Parquets; stock quote-liquidity | Pricing, Options, B, Strategy | `datafetching/databento_fetch.py:541`, `datafetching/equity_dataset_migration.py`, `datafetching/orchestrate.py:292`, `ml/prediction_runtime.py:209` | High |
| Daily ALFRED runtime | Indirect | Indirect | Indirect | Both | verified vintage macro context/readiness; causal FEDFUNDS observation | Pricing and B; Strategy only through B | `datafetching/fred_vintages.py:364`, `datafetching/fred_alfred_readiness.py:667`, `ml/option_pricing/rates.py:361`, `ml/rolling_materialization.py:740` | High for path; Unknown live coverage |
| Active Pricing / logical Loop 3 | Indirect | Direct | Indirect | Both | authoritative constrained Black–Scholes target point values; one-to-one finite-basis residual/fallback sidecar with uncertainty; compact `opx__` surfaces; verified per-leg BSGP/Black–Scholes catalog | Options barrier, B, Strategy | `ml/option_pricing/target_outcome.py:93`, `ml/option_pricing/prediction.py:253`, `ml/rolling_materialization.py:663`, `ml/option_pricing/strategy_shadow.py:298` | High for mechanics; empirical residual lift Unknown |
| Options Capture / logical Loop 4 | Indirect | Indirect | Indirect | Both | immutable canonical live-OPRA/explicit-Schwab-fallback chains and later-target outcomes; daily completed-cursor OPRA history maintenance; compact `opt__` quality features | Pricing, B, Strategy | `options/databento_live.py`, `datafetching/options_runtime.py`, `options/publication.py`, `ml/option_pricing/causal.py`, `ml/strategy_selection/chain.py` | High for implemented provider contracts; production population/share requires current receipts |
| Directional Loop B | Direct | None | Indirect | Both | authoritative samples and LIVE raw/calibrated horizon probabilities with target/action clocks | Strategy and read-only forecast UI | `ml/runtime_pipeline.py:493`, `ml/runtime_pipeline.py:704`, `ml/runtime_pipeline.py:876`, `ml/strategy_runtime.py:74` | High |
| Strategy runtime | None | None | Direct | Options | authoritative calibrated profitable-outcome probability only when fitted with full eligible Pricing coverage, or separate non-probabilistic Scenario Coverage; expected profit/return and rank | read-only Options Strategy UI with research-only gating | `ml/strategy_selection/contracts.py:33`, `ml/strategy_selection/model.py`, `ml/strategy_runtime.py`, `ml/strategy_publication.py` | High for contract; current fitted-model maturity observed insufficient |

## Directional horizon prediction causal chain

1. **Confirmed:** Loop A makes normalized/derived equity evidence and a complete-cycle cutoff authoritative; CME, Daily ALFRED, Options Capture and Active Pricing independently publish cross-asset, macro, `opt__`, and `opx__` evidence. `datafetching/loop_a_cycle.py:127`, `datafetching/cme_cross_asset_context.py:277`, `datafetching/fred_alfred_readiness.py:667`, `options/publication.py:92`, `ml/option_pricing/publication.py:83`
2. **Confirmed:** Loop B holds the shared Loop A lock and materializes only data causally available by that complete-cycle cutoff, enforcing family freshness/readiness and quarantining unavailable Pricing features into the registered baseline. `ml/prediction_runtime.py:209`, `ml/rolling_materialization.py:160`, `ml/runtime_pipeline.py:432`
3. **Confirmed:** target windows use predetermined exchange-calendar constituents; completed observations are split chronologically into training, calibration, assessment and sealed lockbox. `ml/horizons.py:121`, `ml/rolling_samples.py:288`, `ml/model_runtime.py:141`
4. **Confirmed:** Loop B fits or reuses a compatible model, calibrates without assessment/lockbox leakage, scores the current eligible sample, enforces `actionable_until`, then atomically publishes the LIVE probability. `ml/model_runtime.py:372`, `ml/model_runtime.py:466`, `ml/runtime_pipeline.py:509`, `ml/runtime_pipeline.py:603`, `ml/runtime_pipeline.py:876`

## Option-pricing prediction causal chain

1. **Confirmed:** Loop A publishes the exact target readiness and underlying close; an earlier canonical OPRA-or-explicit-Schwab-fallback Options snapshot supplies effective contract and lagged-volatility evidence; causal FRED/ALFRED rates and knowable dividend evidence complete the six semantic inputs. `datafetching/bar_readiness.py:82`, `ml/option_pricing/causal.py:107`, `ml/option_pricing/causal.py:264`, `ml/option_pricing/policies.py:42`
2. **Confirmed:** Active Pricing waits for readiness within the target's 1,200-second window and filters eligible contracts. It publishes constrained Black–Scholes baseline point values with fitted uncertainty null, plus a separate one-to-one `BS + residual` sidecar carrying finite-basis posterior intervals or explicit wider Black–Scholes fallback intervals. `ml/option_pricing_runtime.py:1116`, `ml/option_pricing_runtime.py:1220`, `ml/option_pricing/prediction.py:98`, `ml/option_pricing/prediction.py:253`
3. **Confirmed:** target samples/predictions/status, receipt and pointer publish immutably; the earliest eligible later-target Options snapshot reconciles outcomes only when its exact quote and receipt follow prediction availability, retaining the later target and feeding evaluations/surfaces/full-generation authority. `ml/option_pricing/target_outcome.py:192`, `ml/option_pricing/causal.py:963`, `ml/option_pricing/causal.py:1215`, `ml/option_pricing_runtime.py:2239`

This is the implemented `BLACK-SCHOLES-OP` relationship: the reference's `f(x)=BS(x)+delta(x)` and six inputs are present, while Ducketz uses a bounded Nyström/Bayesian-ridge residual approximation rather than the reference exact GP/MCMC. `docs/edu/BLACK-SCHOLES-OP.md:327`, `docs/edu/BLACK-SCHOLES-OP.md:441`, `ml/option_pricing/model.py:68`

## Options-strategy prediction causal chain

1. **Confirmed:** Strategy binds one verified Loop B run, using its LIVE calibrated direction probability, exact target clocks and feature context. `ml/strategy_runtime.py:74`, `ml/strategy_selection/runtime.py:225`
2. **Confirmed:** for recurring live candidates it selects exact causal prospective provider-neutral option-chain receipts, using OPRA priority and verified Schwab fallback per natural target, and separately uses Schwab stock BBO where required. Canonical OPRA replay/cache may supply offline history/outcomes but is explicitly disabled for live entry and live Pricing attachment. It then constructs policy-eligible candidates and attaches receipt-proven Pricing evidence before any fitted probability score. Ready target sidecars are canonicalized to `BSGP`; complete residual fallbacks are `BLACK_SCHOLES`; unavailable/ineligible coverage cannot masquerade as fitted pricing. `ml/strategy_selection/chain.py:116`, `ml/strategy_selection/runtime.py:167`, `ml/option_pricing/strategy_shadow.py`
3. **Confirmed:** historical exact entry/exit receipts produce strictly-positive-net-profit labels; chronological partitions fit/reuse a classifier/regressor and calibrate on a separate window. `ml/strategy_selection/runtime.py:351`, `ml/strategy_selection/model.py:137`, `ml/strategy_selection/model.py:330`, `ml/strategy_selection/model.py:364`
4. **Confirmed:** eligible candidates receive calibrated profitable-outcome probability and bounded expected profit/return; otherwise only separately typed Scenario Coverage remains and probability fields are null. Rows are deterministically ranked, validated and atomically published. `ml/strategy_selection/model.py`, `ml/strategy_selection/runtime.py`, `ml/strategy_runtime.py`, `ml/strategy_publication.py`

**Observed 2026-08-19 11:15 UTC:** the latest Strategy report had zero
calibrated candidates and 5,760 Scenario Coverage candidates. All nine routes
were `MODEL_NOT_FIT`: the policy needs 378 decision clusters (252 train, 63
calibration, 63 assessment), while only 2 `1h` and 1 `4h` usable clusters were
available and every daily/weekly route had zero. This is insufficient maturity,
not evidence of poor calibrated performance; Scenario Coverage must remain out
of calibrated/raw/decision probability fields.

## Interpretation limits

- `Direct` and `Indirect` classify implemented authority/data paths, not causal
  effect size. A loop can qualify as `Both` even when its current evidence is
  stale, gated out, or empirically unhelpful.
- No documentation statement proves current provider entitlement, datastore
  population, route coverage, model admission, calibration, profitability, or
  realized lift. Those require current receipts, health/consumer-usage records,
  manifests, and chronological evaluation outputs.
- Compatibility mirrors and legacy readers do not create another prediction
  owner. Authority remains the verified pointers named above.
- Historical bootstrap commands, including the all-dataset Databento cold
  start, establish input evidence only. They do not publish a prediction and do
  not qualify as an eighth loop.
- Equity and CME provider archives are not isolated from operational
  continuation: checksum-lineage bridges materialize equity baseline rows and
  CME context/cursor evidence without turning maintenance commands into owners.
- The hourly/daily/weekly monitor describes operational and evaluation health;
  it is read-only and does not create another causal contribution edge.
- `None` means no executable path into that prediction family was found in the
  repository. Untracked external automation remains outside this audit.
