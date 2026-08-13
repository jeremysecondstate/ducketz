# Feature ML Use, Domain, and Importance

## Scope and interpretation

This report summarizes the audited `20260812T182857.767187Z` run documented in `feature-null-tracking.md`. “Selected” means that a feature appeared in an actual fitted logistic-model manifest, not merely in the shared Parquet schema; `1w*` means `1w` plus `1w-d1` through `1w-d5`.

All 22 features had zero non-null values in 92,052 samples, so preprocessing supplied numeric zero plus a constant missingness indicator. Consequently, none provided a varying predictive signal in the audited run, and no defensible learned feature-importance ranking can be extracted from those artifacts.

The importance labels below therefore rank **remediation reach**, not proven predictive value:

- **P1 — highest reach:** selected by all nine model routes.
- **P2 — high reach:** selected by seven model routes.
- **P3 — medium reach:** selected by three model routes.
- **P4 — focused reach:** selected by two model routes.

| Category | Features | Primary role | Model reach | Importance |
| --- | --- | --- | ---: | --- |
| Option-pricing surface | 1–11 | Options-derived metrics consumed by horizon models | 9 routes each | P1 |
| Macro context | 12–15 | Daily/weekly horizon inputs; feature 12 also supports option pricing | 7 routes each | P2 |
| SEC capital-structure events | 16–18 | Daily/weekly horizon inputs | 7 routes each | P2 |
| CME equity-index direction | 19–20 | Intraday/daily horizon inputs | 3 routes each | P3 |
| CME market microstructure | 21–22 | Intraday horizon inputs | 2 routes each | P4 |

## Option-pricing surface features — P1

**1. `opx__causal_coverage` — P1 (highest reach).** **ML use:** Yes—selected by the `1h`, `4h`, `1d`, and `1w*` horizon models, but its all-null state meant no varying signal in the audited run. **Domain:** It is an options-surface completeness/quality output used by the horizon models and as an options-pipeline diagnostic, not an input used to price individual contracts.

**2. `opx__median_normalized_residual` — P1 (highest reach).** **ML use:** Yes—selected by the `1h`, `4h`, `1d`, and `1w*` horizon models, but it supplied no varying signal in the audited run. **Domain:** It is calculated from observed option midpoints versus Black-Scholes prices and then passed to horizon models; it is an option-pricing output, not an option-model input.

**3. `opx__median_predictive_standard_deviation` — P1 (highest reach).** **ML use:** Yes—selected by the `1h`, `4h`, `1d`, and `1w*` horizon models, but it supplied no varying signal in the audited run. **Domain:** It is an uncertainty output from the option fair-value model that is subsequently used as a horizon-model input, not a feature used to generate the option prediction itself.

**4. `opx__median_model_edge_in_half_spreads` — P1 (highest reach).** **ML use:** Yes—selected by the `1h`, `4h`, `1d`, and `1w*` horizon models, but it supplied no varying signal in the audited run. **Domain:** It is an options valuation/trading metric derived from modeled fair value and market quotes, then consumed by the horizon models rather than used to train the option-pricing model.

**5. `opx__positive_edge_fraction` — P1 (highest reach).** **ML use:** Yes—selected by the `1h`, `4h`, `1d`, and `1w*` horizon models, but it supplied no varying signal in the audited run. **Domain:** It summarizes how broadly option fair values exceed market midpoints and is an options-derived input to horizon predictions, not an option-pricing-model input.

**6. `opx__negative_edge_fraction` — P1 (highest reach).** **ML use:** Yes—selected by the `1h`, `4h`, `1d`, and `1w*` horizon models, but it supplied no varying signal in the audited run. **Domain:** It summarizes how broadly option fair values fall below market midpoints and is an options-derived input to horizon predictions, not an option-pricing-model input.

**7. `opx__raw_arbitrage_violation_rate` — P1 (highest reach).** **ML use:** Yes—selected by the `1h`, `4h`, `1d`, and `1w*` horizon models, but it supplied no varying signal in the audited run. **Domain:** It is primarily an option-model/surface quality diagnostic that is also fed to the horizon models; it does not produce option prices.

**8. `opx__constrained_arbitrage_violation_rate` — P1 (highest reach).** **ML use:** Yes—selected by the `1h`, `4h`, `1d`, and `1w*` horizon models, but it supplied no varying signal in the audited run. **Domain:** It checks the option surface after constraints and is primarily an options quality diagnostic reused by the horizon models, not an option-pricing input.

**9. `opx__interval_80_coverage` — P1 (highest reach).** **ML use:** Yes—selected by the `1h`, `4h`, `1d`, and `1w*` horizon models, but it supplied no varying signal in the audited run. **Domain:** It is an option-model calibration/eligibility diagnostic computed after evaluation and then passed to horizon models, not an input used to predict option fair value.

**10. `opx__interval_95_coverage` — P1 (highest reach).** **ML use:** Yes—selected by the `1h`, `4h`, `1d`, and `1w*` horizon models, but it supplied no varying signal in the audited run. **Domain:** It is an option-model calibration/eligibility diagnostic computed after evaluation and then passed to horizon models, not an input used to predict option fair value.

**11. `opx__median_relative_bid_ask_spread` — P1 (highest reach).** **ML use:** Yes—selected by the `1h`, `4h`, `1d`, and `1w*` horizon models, but it supplied no varying signal in the audited run. **Domain:** It is an options-liquidity metric derived from option quotes and consumed by horizon models, not an input to the option fair-value model.

## Macro context features — P2

**12. `macro__fed_funds_level` — P2 (high reach).** **ML use:** Yes—selected by the `1d` and `1w*` horizon models, but it supplied no varying signal in the audited run. **Domain:** It has dual use: macro context for time-horizon prediction and a risk-free-rate input to the option-pricing pipeline.

**13. `macro__cpi_yoy` — P2 (high reach).** **ML use:** Yes—selected by the `1d` and `1w*` horizon models, but it supplied no varying signal in the audited run. **Domain:** It is a macro input for daily and weekly time-horizon predictions, with no identified role in option pricing.

**14. `macro__unemployment_change` — P2 (high reach).** **ML use:** Yes—selected by the `1d` and `1w*` horizon models, but it supplied no varying signal in the audited run. **Domain:** It is a macro input for daily and weekly time-horizon predictions, with no identified role in option pricing.

**15. `macro__gdp_yoy` — P2 (high reach).** **ML use:** Yes—selected by the `1d` and `1w*` horizon models, but it supplied no varying signal in the audited run. **Domain:** It is a macro input for daily and weekly time-horizon predictions, with no identified role in option pricing.

## SEC capital-structure event features — P2

**16. `sec__dilution_event` — P2 (high reach).** **ML use:** Yes—selected by the `1d` and `1w*` horizon models, but it supplied no varying signal in the audited run. **Domain:** It is a sparse SEC-event input for daily and weekly time-horizon predictions, not an option-pricing feature.

**17. `sec__offering_size_to_market_cap` — P2 (high reach).** **ML use:** Yes—selected by the `1d` and `1w*` horizon models, but it supplied no varying signal in the audited run. **Domain:** It is a normalized SEC-offering input for daily and weekly time-horizon predictions, not an option-pricing feature.

**18. `sec__filing_event_impulse` — P2 (high reach).** **ML use:** Yes—selected by the `1d` and `1w*` horizon models, but it supplied no varying signal in the audited run. **Domain:** It is a sparse SEC filing-event input for daily and weekly time-horizon predictions, not an option-pricing feature.

## CME equity-index direction features — P3

**19. `cme__nq_return_1h` — P3 (medium reach).** **ML use:** Yes—selected by the `1h`, `4h`, and `1d` horizon models, but it supplied no varying signal in the audited run. **Domain:** It is Nasdaq-100 futures context for time-horizon predictions, not an option-pricing feature.

**20. `cme__es_return_1h` — P3 (medium reach).** **ML use:** Yes—selected by the `1h`, `4h`, and `1d` horizon models, but it supplied no varying signal in the audited run. **Domain:** It is S&P 500 futures context for time-horizon predictions, not an option-pricing feature.

## CME market-microstructure features — P4

**21. `cme__relative_spread` — P4 (focused reach).** **ML use:** Yes—selected by the `1h` and `4h` horizon models, but it supplied no varying signal in the audited run. **Domain:** It is futures-market liquidity context for intraday time-horizon predictions, not an option-pricing feature.

**22. `cme__book_imbalance` — P4 (focused reach).** **ML use:** Yes—selected by the `1h` and `4h` horizon models, but it supplied no varying signal in the audited run. **Domain:** It is futures order-book pressure context for intraday time-horizon predictions, not an option-pricing feature.

## Bottom line

All 22 features are configured as inputs to one or more time-horizon logistic models. Features 1–11 originate in the options-pricing pipeline, feature 12 also supports option pricing directly, and features 13–22 are horizon-prediction context only; none of the 22 contributed usable variation in the audited run.
