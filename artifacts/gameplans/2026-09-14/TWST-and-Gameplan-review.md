# TWST and Gameplan review — September 14, 2026

## Why TWST repeatedly bought and sold

The saved plan contains **six buys and six sells**. Five sales close the previous one-hour position at its scheduled end. Only the final 13:00 sale is classified as a bearish forecast sale. Each hour has its own holding window; the current strategy closes the old allocation even when the next hour is also bullish.

For example, the 06:00 forecast was bullish at 58.22%. Its position was scheduled to close at 07:00. The separate 07:00 forecast was also bullish, at 55.34%, so the projection immediately opened a new one-hour position. Actual execution waits for the prior exit to fill before opening that next allocation.

| Planned entry, Pacific | Published P(up) | Planned shares | Planned close | Sale reason |
| --- | ---: | ---: | --- | --- |
| 06:00 | 58.22% | 14 | 07:00 | Scheduled horizon exit |
| 07:00 | 55.34% | 14 | 08:00 | Scheduled horizon exit |
| 09:00 | 54.46% | 14 | 10:00 | Scheduled horizon exit |
| 10:00 | 59.76% | 14 | 11:00 | Scheduled horizon exit |
| 11:00 | 54.83% | 14 | 12:00 | Scheduled horizon exit |
| 12:00 | 60.60% | 15 | 13:00 | Bearish sale; 13:00 P(up) was 40.54% |

The repeated 14-share estimate comes from the same allocation: $127,811.59 account equity × 15% symbol cap × 1/10 hourly weight = approximately **$1,917.17**. Dividing that budget by similar upper planning prices and rounding down gives 14 shares for the first five entries. The last projected entry is 15 shares. Current cash, quotes and holdings determine actual quantities.

The underlying saved report is [Gameplan.md](C:/DATASTORE/ml/gameplan-trade-plan-runs/20260914T124427.308015Z/Gameplan.md). The six entry probabilities and all-company counts were checked against the saved forecast rows and direction ledger.

## Why All companies showed only TWST

All eleven companies are present: **264 forecasts, including 209 eligible entry windows**. The tab opened in Trades mode. TWST was the only company with bullish eligible forecasts; the other ten had bearish or neutral eligible forecasts and zero shares available to sell in the planning snapshot. They therefore had no projected orders to display in that mode. Some bullish opening-gap research rows exist for other symbols, but those rows are context rather than entries.

The UI now opens in All forecasts. Selecting All companies also returns to that view. Explicitly selecting Trades is still supported and survives refresh. The trade view states how many companies have projected actions, labels timed sales **EXIT**, and separates scheduled exits from bearish sales. The exit inspector identifies the originating entry forecast.

Restart the Duckets application once to load the updated UI code. Refresh reloads saved data within the running application.

[All-company preview](C:/dev/ducketz/artifacts/gameplans/2026-09-14/gameplan-all-companies.png) · [Trade view preview](C:/dev/ducketz/artifacts/gameplans/2026-09-14/gameplan-trades-explained.png)

## What actually executed today

Read-only inspection of the native holdings ledger confirms **five filled TWST round trips**, with entry quantities 15, 14, 14, 14 and 14: 71 shares bought and 71 sold. No allocation remained active at review time. The repeated projections in the Gameplan are separate from these recorded fills.

The 06:00 instruction was missed. Its 06:01 decision recorded `CURRENT_QUOTE_TOO_OLD`. The 06:50 recovery selected an order but stopped locally on `QUEUE_TARGET_NO_LONGER_MATCHES_CURRENT_WINDOW`. The later 07:01 fill belongs to the separate 07:00 instruction. The detailed [session closing report](C:/DATASTORE/logs/daytime-operations/20260914T105918Z-codex-supervision/session-closing-report.md) preserves those failures and the actual fills.

## Execution and tonight's setup

Today's existing execution fixes were retained and tested:

- Gameplan execution reads the saved instructions without revalidating research receipts, model files or planning-price ranges.
- Orders use current broker prices: ask for buys, bid for regular-session sales, and the configured midpoint behavior for wide spreads. A fresh real-time NBBO response can retain an older provider update time.
- Unsubmitted quote failures can retry within the original forecast hour. Forecast reservations and recorded allocations prevent duplicates.
- Due exits use actual owned shares. A later entry waits for the prior allocation to close; pending sale proceeds do not fund buys.
- The actual saved loader returned all 209 eligible instructions across all eleven companies and four horizons.

The existing overnight task is enabled for **21:05 Pacific on September 14**. Native calendar checks accept that completed session. The normal [manual launcher](C:/dev/ducketz/Start-Gameplan-Trader.cmd) selects Gameplan execution, waits for the next supported **04:00 Pacific September 15** opening, then begins normal entries at **04:01**. No overnight or trader process was running during this review. Tomorrow's publication is pending tonight's scheduled run; future broker acceptance and fills have not been observed.

Concise findings were recorded in the existing overnight and daytime tasks' continuity notes. This review changed UI presentation, documentation and tests. It added no production trading checks and changed no trading policy, activation control, schedule, saved forecast or broker order.

## Validation

- Focused Python suite: 368 passed, one UI test skipped. That exact UI test passed in an isolated rerun; an earlier complete UI/data run also passed all 58 tests.
- 56 synthetic PowerShell launcher identity checks passed.
- Added integration coverage exercises three consecutive bullish TWST windows through the real saved-instruction loader, current-price selection, simulated broker fills, exits, next-hour entries and duplicate suppression. It includes entering the 06:00 forecast at 06:50 after the premarket-to-core transition.
- Rendered and visually checked the all-company and trade views against the actual saved September 14 plan.
- All execution verification used synthetic brokers. No live broker request was made during this review.

Machine-readable evidence: [verification.json](C:/dev/ducketz/artifacts/gameplans/2026-09-14/verification.json).
