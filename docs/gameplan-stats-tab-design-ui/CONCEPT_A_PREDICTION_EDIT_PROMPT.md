# Concept A — prediction-performance revision

Mode: built-in imagegen edit. Target: C:/dev/ducketz/docs/gameplan-stats-tab-design-ui/concept-a-session-scorecard.png. The original image is retained as concept-a-session-scorecard-v1-planning-ranges.png before replacing the requested target.

Numerical input: concept-a-prediction-metrics-september-10.json, calculated from the verified saved forecast-results.parquet. No app or trading changes.

## Final prompt

+Use case: precise-object-edit (desktop UI mockup).
Input image 1 is the EDIT TARGET: existing Concept A Session Scorecard. Preserve its native Duckets title bar, horizontal tab strip with Gameplan Stats selected fifth, dark navy panels, cyan selection outline, company logos, compact typography, exact overall layout and panel sizes. This is an update to this image, not a new layout. Make crisp, legible professional typography at roughly the original landscape proportions.

PRIMARY CHANGE: Remove every planning-price or price-range metric and replace them with actual saved-model prediction performance. Do not leave any mention of range hits, prices in range, price-range map, mean absolute price error, price coverage, inside/outside saved range, or the old 22.9%,1.87%,96/98 numbers. Keep the date Sep 10, 2026; All horizons; Open full report; Refresh. Subtitle: "Saved predictions compared with observed outcomes. All times Pacific."

FOUR TOP KPI CARDS (same size/placement as original; neutral white values, cyan icons):
1 "Direction accuracy" / "54.0%" / "47 / 87 scored calls"
2 "Probability error" / "0.222" / "Brier score · lower is better"
3 "Bullish accuracy" / "25.0%" / "1 / 4 scored calls"
4 "Bearish accuracy" / "55.4%" / "46 / 83 scored calls"
Do NOT format Brier as a percentage. This is a probability prediction error, not a calibration percentage.

COMPANY SCORECARD: preserve left middle table, seven logos/rows, AAPL selected. Five columns:
"Symbol" | "Direction accuracy" (existing horizontal bars and percentage) | "Correct / Scored" | "Brier score ↓" | "Bull / Bear calls".
Copy these exact rows:
AAPL |40.0%|6 / 15|0.246|0 / 15
AMZN |56.3%|9 / 16|0.183|0 / 16
COST |46.2%|6 / 13|0.262|0 / 13
GOOG |60.0%|9 / 15|0.193|0 / 15
MU |62.5%|5 / 8|0.229|1 / 7
NVDA |71.4%|10 / 14|0.206|1 / 13
SNDK |33.3%|2 / 6|0.248|2 / 4
Here "Bull / Bear calls" is the mix of scored predictions, not correct/total. Align column values precisely.

RIGHT MIDDLE TOP panel stays "Outcome coverage", "Total:168", "121 evaluated", "42 awaiting maturity", "5 awaiting price data". Its proportional strip is about72% green/25% amber/3% gray. Add a small line "Brier uses all 121 evaluated forecasts."

RIGHT MIDDLE BOTTOM panel keeps "AAPL · selected" and two inset metric boxes:
left "6 / 15" / "correct / scored" / "40.0% direction accuracy"
right "0.246" / "Brier score" / "18 evaluated forecasts"
Below these boxes small text: "0 bullish · 15 bearish · 3 neutral".
No remnants of original price-range legend.

BOTTOM PANEL: preserve the same large compact seven-row grid style, but title becomes "1-hour prediction outcomes". This is ACTUAL CORRECTNESS of a saved one-hour directional prediction, not price-range membership. Use thirteen columns, not fourteen, labeled with the forecast START–END window:
"04–05","05–06","06–07","07–08","08–09","09–10","10–11","11–12","12–13","13–14","14–15","15–16","16–17".
Add a compact legend at upper right:
green check = "Correct", coral cross = "Incorrect", muted gray dash = "Neutral", amber question mark = "Awaiting data".
Use the SAME status sequences below, exactly13 cells per row in listed window order. C=green check, X=coral cross, N=muted gray dash, ?=amber question mark.
AAPL: C C N X C X X X X C X C C
AMZN: C C X X C X C C X C X C C
COST: X X C C X X C X X ? C ? ?
GOOG: C C C X C X X X X C C C C
MU: N C N N N C X N N C X C C
NVDA: C C C X X C C C X C X C C
SNDK: N N X N N N X N N N X C N
Each grid cell pairs a symbol and color to avoid relying only on color. Do NOT copy old range-map colors or dots. Keep company identities in the left grid column.

OUTER FOOTER: "Direction:87 scored calls; 34 neutral excluded. Brier:121 evaluated forecasts, including neutral. Pending/missing excluded." Add discreet scope note "Grid:1h windows · Summary:all saved horizons" and label "CONCEPT A · PREDICTION SCORECARD". It is acceptable to use two thin footer lines if needed, without clipping the grid.

No profit/P&L, invented performance, price estimates, research claims, trading buttons, new sidebar, decorative charts, handwriting or yellow markings. All stated numeric values and grid statuses are verified from the saved September10 results. Render ONLY the polished edited desktop concept image.
# Grid refinement

Precise localized edit of the supplied desktop UI screenshot. Keep every pixel outside the bottom '1-hour prediction outcomes' GRID unchanged, including all title bars, tabs, KPI cards, scorecard table, selected company, coverage panel and footer. The grid currently has fourteen columns due to a duplicate '13–14' column. DELETE the LEFT of the two '13–14' columns, meaning the entire column immediately after '12–13', including its seven cells. That erroneous extra column has AAPL red cross, AMZN green check, COST red cross, GOOG red cross, MU neutral dash, NVDA red cross, SNDK neutral dash. Keep the RIGHT '13–14' column, which has AAPL green check, AMZN green check, COST amber ?, GOOG green check, MU green check, NVDA green check, SNDK neutral dash. Then redistribute the THIRTEEN remaining columns evenly across the same grid width. Final headers must each appear ONCE:04–05,05–06,06–07,07–08,08–09,09–10,10–11,11–12,12–13,13–14,14–15,15–16,16–17. Preserve the seven company rows. Final exact13 cell statuses in header order, C=green check,X=coral cross,N=gray dash,?=amber question mark: AAPL:C C N X C X X X X C X C C; AMZN:C C X X C X C C X C X C C; COST:X X C C X X C X X ? C ? ?; GOOG:C C C X C X X X X C C C C; MU:N C N N N C X N N C X C C; NVDA:C C C X X C C C X C X C C; SNDK:N N X N N N X N N N X C N. ONLY correct the duplicated grid column; make no other changes.
# Final grid layout — thirteen windows plus score column

+Edit only the bottom grid of this desktop UI. Preserve EVERYTHING above it and the exact existing fourteen-column geometry. Do not delete or insert any columns. Make the first THIRTEEN columns the thirteen unique hourly windows, and the FOURTEENTH column a neutral-text per-row score summary.

There are precisely14 headers, one per existing column, left to right:
1=04–05;2=05–06;3=06–07;4=07–08;5=08–09;6=09–10;7=10–11;8=11–12;9=12–13;10=13–14;11=14–15;12=15–16;13=16–17;14=1h score.
Thus change the current duplicate second15–16 header (column13) to16–17, and change the last header(column14) from16–17 to1h score.

In columns1–13 render C=green check, X=coral cross, N=gray dash, ?=amber question mark. Column14 must have a dark neutral background with the white fraction indicated, no symbol.
AAPL: C C N X C X X X X C X C C | 6/12
AMZN: C C X X C X C C X C X C C | 8/13
COST: X X C C X X C X X ? C ? ? | 4/10
GOOG: C C C X C X X X X C C C C | 8/13
MU: N C N N N C X N N C X C C | 5/7
NVDA: C C C X X C C C X C X C C | 9/13
SNDK: N N X N N N X N N N X C N | 1/4
Pay special attention to SNDK column13 (16–17): it is NEUTRAL, a gray dash, not a green check. All seven far-right cells are score fractions, not hourly icons.
Keep the legend/title/footer and all other parts unchanged. This is a single small grid-label and last-column correction, NOT a redesign.
