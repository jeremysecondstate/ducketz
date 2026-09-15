# Removal of the stock forecast neutral band

Implemented September 14, 2026 evening, before the next scheduled 21:05 Pacific overnight run.

## User-selected rule

| Saved probability | Direction |
| --- | --- |
| Below 50% | Bearish |
| Exactly 50% | Neutral / no directional signal |
| Above 50% | Bullish |

New publications record `stock-direction-50-v2`, with both direction thresholds set to 0.50. Classification uses the full saved probability, before display rounding. The previous 46%–54% neutral band is removed for new forecasts.

The shared direction policy, nightly publication, conditional trade ledger, forecast loaders, current-market Gameplan execution and fixed-budget forecast readiness now agree. The default stock trader minimum probability is 0.50; fixed-budget readiness additionally requires a strictly bullish forecast. A deliberately configured stricter minimum remains an explicit separate policy setting. The manual Gameplan strategy uses the shared direction rule directly.

Model approval remains separate from direction. Historical saved plans, prediction values, labels, cutoffs and actual results were not rewritten. Report rendering retains the old inclusive bands when an older report supplies them; new reports describe the strict 50% split and exact tie correctly. Stats score each saved call using its original label and probability.

## Trading and measurement

Synthetic execution verified 51% buys and 49% sells using current quote prices. The integration also exercised three consecutive entries, actual simulated fills, scheduled exits, cash accounting and duplicate suppression through the saved forecast loader. Simulated broker only; this change did not start a trader or submit real orders.

The current-market Gameplan path continues to use current executable quotes, actual cash and reconciled eligible shares. Existing horizon ownership, exits and duplicate prevention apply. Bearish forecasts sell eligible held shares; they do not open shorts. Trade history and realized P&L require fills, while forecast accuracy can be measured from observed price outcomes without a trade. Removing the neutral band expands directional calls; it does not establish improved accuracy or profitability.

## Verification

- The 470-case focused regression run passed 469 cases. Its only remaining failure was the new historical-scoring test assuming every hourly grid cell contained a forecast; the grid correctly includes empty cells. The test was corrected to inspect populated cells.
- The subsequent 99-case run passed in full, including the complete Gameplan Stats data suite, stock trader tests, nightly publication tests and target-quality tests. This includes the corrected historical-scoring regression. No production edits followed these runs.
- Focused coverage included the new probability boundaries, planning, all 11 symbols in synthetic execution, consecutive horizon fills/exits, quote recovery, legacy reports, saved UI data and Gameplan UI.
- A pre-existing fixed-budget test assumed seven companies; its assertion now uses the configured symbol count while retaining its six-order-cap checks.
- `git diff --check` passed.

## Scheduled operation

The existing daytime supervision automation was updated through the Codex app to replace its old 46%/54% instruction. Read-back confirmed that only its prompt and update timestamp changed; its schedule, activation, model, project and notification settings were preserved. The overnight and daytime task memories carry this change and validation summary. The saved production prediction pointer still referred to September 14 during implementation; tomorrow's forecasts are produced by the next overnight run.

Relevant sources: [shared direction policy](C:/dev/ducketz/ml/stock_direction_policy.py), [nightly publication](C:/dev/ducketz/ml/nightly_gameplan.py), [current-market execution](C:/dev/ducketz/ml/stock_trader/gameplan_direction_engine.py), [runtime integration tests](C:/dev/ducketz/tests/test_gameplan_execution.py), and [operating documentation](C:/dev/ducketz/docs/loops-system-analysis/INDEPENDENT_STOCK_HORIZONS.md).

The automation update used the OpenAI Docs skill and the native automation tool, following the [official scheduled-task documentation](https://learn.chatgpt.com/docs/automations?surface=app#ask-chatgpt-to-create-or-update-scheduled-tasks).
