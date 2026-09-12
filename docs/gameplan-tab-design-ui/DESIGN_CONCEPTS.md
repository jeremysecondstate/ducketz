# Gameplan tab — design concepts

Three visual directions for a Gameplan tab, beside Gameplan Stats. The concepts use the saved Monday, September 14, 2026 plan and the approved Stats tab's navy, cyan, logo and table styling. Concept A has now been implemented; see [implementation and verification](IMPLEMENTATION.md). The original concept images remain available for reference.

## A — Session run sheet

[View concept A](concept-a-session-run-sheet.png)

The closest companion to the approved Stats layout. Summary tiles establish the next planned batch, entries, exits and horizon coverage. A chronological table exposes every projected trade, a selected-trade inspector explains its forecast and planned lifecycle, and a lower strip keeps all four horizons visible.

**Recommended starting point:** it answers “what is planned, when, and why?” with the least navigation. The six-event example fits on screen; busier plans would use a scrollable agenda with date, company and horizon filters. A forecast view exposes the hold windows as well.

## B — Horizon timeline

[View concept B](concept-b-horizon-timeline.png)

A schedule-first alternative. Horizon lanes make timing, planned holding periods and overlapping windows easy to compare. Grey forecast windows are visually distinct from projected holdings. A complete batch list below the chart keeps individual quantities readable.

This is strongest for inspecting overlap and future expiries. The late four-hour window crosses into Tuesday; the weekly window ends Friday. A session/week switch would expose those dates without squeezing every day onto one axis.

## C — Company playbooks

[View concept C](concept-c-company-playbooks.png)

A company-first alternative. Select a company to inspect its forecasts and planned actions by horizon, then follow its entry-to-exit journey. The agenda remains global so company selection does not hide other planned trades.

The four opening cards explicitly describe the 04:00 decision, rather than implying one forecast covers every hourly window. A full-forecast control provides access to later windows and outlook context.

## Data used in the concepts

All times are Pacific. Saved plan: September 14, 2026. Published September 11 at approximately 22:13 PDT.

| Time, Sep 14 | Company | Projected action | Horizon | Shares | Planning price | Reason |
| --- | --- | --- | --- | ---: | ---: | --- |
| 04:00 | GOOG | BUY | 1d | 17 | $335.33 | Bullish entry |
| 04:00 | AAPL | BUY | 1d | 17 | $332.49 | Bullish entry |
| 04:00 | AMZN | BUY | 1d | 22 | $257.02 | Bullish entry |
| 17:00 | AAPL | SELL | 1d | 17 | $333.66 | Horizon expiry |
| 17:00 | AMZN | SELL | 1d | 22 | $256.44 | Horizon expiry |
| 17:00 | GOOG | SELL | 1d | 17 | $335.52 | Horizon expiry |

These six events come from the direction ledger, which applies directions in sequence against shared projected cash and per-horizon holdings. Standalone capacity quantities are not scheduled trades. The projected exits assume the corresponding entries filled; actual obligations must follow actual fills.

The plan contains 168 forecasts for seven companies, including 133 entry windows: 91 hourly, 28 four-hour, seven daily and seven weekly. Only three daily entry rows produce buys in this saved projection; the other 130 entry rows produce holds. The remaining 35 forecasts are opening-gap research or later daily outlook, not entry windows.

The latest four-hour window starts Monday, September 14 at 16:00 and ends Tuesday, September 15 at 07:00, spanning four trading hours. The weekly window starts Monday at 04:00 and ends Friday, September 18 at 17:00. Continuation markers in B describe those forecast windows, not active holdings.

For the selected AAPL opening decision, published P(up) is 45.71% for 1h, 45.58% for 4h, 54.36% for 1d and 44.16% for 1w. The three bearish opening forecasts project holds because there are no eligible shares allocated to those horizons. A bullish/bearish direction and a buy/sell/hold action must remain separate fields.

Planning prices are saved estimates. The concepts make no claims about current quotes, submitted orders, fills, trader health or realized profit.

## Interaction principles for a future build

- Default to the next saved session and expose the plan's publication time.
- Keep all projected entries and all projected exits reachable across every horizon, including exits beyond the selected session.
- Distinguish the saved projection from any future live execution overlay. A live overlay would require its own timestamp and actual filled quantities.
- Show forecast direction, published probability, projected action, quantity, entry time, expiry time and action reason as separate information.
- Allow filtering by date, horizon and company, with an obvious count and reset state. A selected-company inspector must not silently filter the global agenda.
- Preserve no-action windows and their reasons in an expandable forecast view. Later daily outlook must not appear as a due order.
- Show full dates for cross-session windows and keep the Pacific timezone visible.
- Keep forecast performance in Gameplan Stats; this tab focuses on the upcoming plan.

## Provenance and generation

Source run: `C:/DATASTORE/ml/gameplan-trade-plan-runs/20260912T051328.518994Z`.

Source files: `direction-ledger.json`, `trade-plan.parquet`, and `Gameplan.md`. The run receipt checksum matched the latest pointer when inspected. The [data extract](plan-design-data-september-14.json) records the source paths, checksums, events and forecast windows used to ground the designs.

The three PNGs were generated with the built-in imagegen tool using the approved [Gameplan Stats concept](../gameplan-stats-tab-design-ui/concept-a-session-scorecard.png) as a style reference. The [complete prompts](CONCEPT_PROMPTS.md) are saved for iteration. Generated text and fine chart geometry are design illustrations; the data extract is the precise source for a future implementation.
