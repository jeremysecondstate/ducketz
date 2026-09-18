# Native app tab verification

Verified at 2026-09-18T06:14:07.9298254+00:00 following the user's request to fully display tomorrow's Gameplan and today's Gameplan Stats.

- Opened the Duckets native app using the repository virtualenv's app.main ui entry point because no app window was open. No trader was launched.
- Gameplan is September 18, 2026: 264 forecasts, 24 for each of 11 companies, four horizons, 209 entry windows and 55 context rows. Its 33 projected actions comprise 30 buys and 3 sells. All 154 hourly price points are present. The carried CROX close disclosure is visible.
- Gameplan Stats is September 17, 2026: 264 saved forecasts, 164 evaluated (62 correct; 37.8% direction accuracy; Brier 0.2412877260), 34 awaiting price data and 66 pending target maturity. Missing and pending outcomes remain explicit.
- Independently verified all 60 company/horizon filter combinations for each adapter, all 143 hourly-grid cells, saved references and counts; see tab-data-audit.json.
- Visually verified the actual app's dates, metrics, coverage, company scorecards, full hourly grid, all-forecasts view and final rows of the projected-trades table. Scrolling reaches the lower content.
- Left the app open on Gameplan, September 18, All horizons, All companies, All forecasts 264, at the top. Stats remains on September 17, All horizons, at the top when selected.

No UI defect or code repair was necessary. No production artifacts, trading controls, schedules or order state were changed.
