# Gameplan Stats — three design concepts

**Implemented:** The revised prediction-focused Concept A is now the fifth desktop tab. See [implementation and validation notes](C:/dev/ducketz/docs/gameplan-stats-tab-design-ui/IMPLEMENTATION.md) and the [rendered September 11 tab](C:/dev/ducketz/docs/gameplan-stats-tab-design-ui/implemented-gameplan-stats-september-11.png). The images below remain the design references.

Created September 11, 2026 for review before implementation. All three concepts retain the existing dark desktop styling and add **Gameplan Stats** as the fifth tab, after Hyperliquid Duckets.

**Current revision:** Concept A now focuses on saved-model prediction performance. Its range and planning-price measures were replaced with Brier probability error, bullish/bearish accuracy, and actual one-hour prediction outcomes. See [current metric definitions and verification](C:/dev/ducketz/docs/gameplan-stats-tab-design-ui/CONCEPT_A_PREDICTION_METRICS.md). B and C remain the earlier alternatives.

| Concept | Main layout | Best use |
|---|---|---|
| [A — Prediction Scorecard](C:/dev/ducketz/docs/gameplan-stats-tab-design-ui/concept-a-session-scorecard.png) | Direction accuracy, Brier probability error, bullish/bearish accuracy, company comparison and a one-hour prediction-outcome grid. | Comparing saved model predictions with observed outcomes. |
| [B — Price Comparison](C:/dev/ducketz/docs/gameplan-stats-tab-design-ui/concept-b-price-comparison.png) | Company selector, a large saved-range/actual-price chart, coverage details and a same-clock comparison table. | Examining where one company's saved estimates matched or diverged from observed prices. |
| [C — Company Cards](C:/dev/ducketz/docs/gameplan-stats-tab-design-ui/concept-c-company-cards.png) | Seven company cards with logos, direction accuracy, range counts and small chart previews; an eighth card shows outcome coverage. | A more visual company-by-company scan. |

**Recommended direction:** The revised A as the default overview. Any later company-detail design should follow the user's preference for model prediction performance. These are static image concepts; no application implementation or trading behavior was changed.

## Original proposed behavior — B/C and A version 1

- Select the reviewed session date and forecast horizon.
- Keep direction accuracy, price-range hits, absolute price error and observation coverage separate; they have different meanings and denominators.
- Show both the target clock and the actual observation timestamp in the detailed comparison, with Pacific time clearly labeled.
- Display unavailable observations and forecasts awaiting maturity separately from incorrect forecasts.
- Open the underlying verified report and expose concise metric definitions.

The drawings show the all-horizons view. In an implementation, a horizon filter should affect only metrics with that horizon dimension; session-wide hourly prices and aggregate coverage should stay explicitly labeled.

## Original numerical reference — B/C and A version 1

The headline and company figures are anchored to the saved **September 10, 2026** review:

| Metric | Value | Population |
|---|---:|---|
| Direction accuracy | 54.0% | 47 correct of 87 scored directional calls |
| Prices inside the saved range | 22.9% | 22 of 96 observed hourly prices |
| Mean absolute price error | 1.87% | Observed hourly price comparisons |
| Price coverage | 96 / 98 | Seven companies, 04:00–17:00 Pacific |
| Forecast outcomes | 121 evaluated, 42 awaiting maturity, 5 awaiting price data | 168 forecasts |

The 121 evaluated forecasts include 34 neutral outcomes excluded from directional accuracy. Two unavailable hourly price observations belong to COST at 15:00 and 17:00. Hourly price observations and forecast outcomes are different populations. These measures describe forecast performance, not realized trading profit.

**Visual accuracy boundary:** The written numerical labels use the saved review. Generated line charts, sparklines, rings and proportional bars are illustrative layout elements, not exact data plots. In particular, B's line geometry and C's mini-charts should not be used to read historical prices. Production charts must be plotted directly from the recorded observations. Company marks are generated visual placeholders.

## Sources and generation

- [Verified result run](C:/DATASTORE/ml/gameplan-actuals-review-runs/20260911T061544.303359Z)
- [Saved Gameplan](C:/DATASTORE/ml/nightly-gameplan-runs/20260910T051155.074940Z)
- [Saved trade plan](C:/DATASTORE/ml/gameplan-trade-plan-runs/20260910T051300.155664Z)
- [Numerical reference snapshot](C:/dev/ducketz/docs/gameplan-stats-tab-design-ui/concept-data-september-10.json)
- [Full generation prompts](C:/dev/ducketz/docs/gameplan-stats-tab-design-ui/CONCEPT_PROMPTS.md)
- [User's tab-placement screenshot](C:/dev/ducketz/docs/gameplan-stats-tab-design-ui/yellow-markup-new-tab.png)
- [Prior design-look reference](C:/dev/ducketz/docs/rolling-forecasts-design-ui/concept-quick-look-a-signal-matrix.png)

Generated with the built-in imagegen tool, using the two supplied screenshots as visual references. Final PNGs were visually reviewed and copied into this folder. The original supplied screenshot remains intact.

## Nightly schedule check

The saved **Loops Overnight Gameplan** automation is ACTIVE at **21:05 Pacific daily**. Its native full-pipeline command includes Gameplan publication, trade planning and the actuals-review stage. On Friday, September 11, the scheduled work is the next supported trading session's **Monday, September 14 Gameplan** and the **September 11 results review**. The **Loops Operations Watch** automation is also active every 90 minutes.

This records the schedule configuration verified during this design task, not a claim that the upcoming run has completed. No schedule edits were needed.
