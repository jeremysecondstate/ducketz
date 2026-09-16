# September 16 Gameplan overnight completion

Native preparation completed at **2026-09-15 23:15:09 Pacific** (September 16 06:15:09 UTC), ahead of the original **September 16 04:00 Pacific** deadline. All eight stages are complete across the original attempt and one focused actuals resume. Eleven active symbols have **264 forecasts, 264 stock-only intents and 264 augmented planning rows**. Overnight orders: **0**. No trader was started and no trading controls, schedules, thresholds, raw evidence or immutable publications were changed.

- Original attempt: `C:/DATASTORE/ml/overnight-runs/20260916T040757.217937Z`.
- Terminal resume: `C:/DATASTORE/ml/overnight-runs/20260916T061451.489725Z`.
- [Readable September 16 Gameplan](C:/DATASTORE/ml/gameplan-trade-plan-runs/20260916T060421.076865Z/Gameplan.md).
- [September 15 actuals](C:/DATASTORE/ml/gameplan-actuals-review-runs/20260916T061453.637702Z/Gameplan-results.md).

## Material model and planning limitations

The 1h, 4h and 1d directional models are promoted. The **weekly model failed Brier and log-loss assessment** and remains `RESEARCH_NOT_PROMOTED`. Independent investigation found no implementation defect or justified immediate retrain: all 2,243 common cohort rows are unchanged, seven legitimate outcomes matured, and rolling calibration development selected identity instead of the prior Platt calibration. Assessment gates and the saved result were preserved. See [weekly diagnosis](C:/dev/ducketz/artifacts/analysis/overnight-20260916/weekly-model-diagnosis.md).

The selected manual Gameplan policy **still reads all eleven weekly opening instructions**, including bullish AAPL, GOOG, MU, NVDA and SNDK. The research label is not an automatic execution block under that policy. The other 198 entry windows are bearish. The legacy promoted-only policy and informational cash projection exclude research-model rows. Learned enrichment sizing is unused by the manual/fixed quantity policies; qualified scopes are 137/143 hourly and zero for the other three horizons.

Current planning prices are available for ten symbols, including IONQ. PATH has fourteen `UNAVAILABLE_REFERENCE_PRICE` points from the existing undefined-native-price guard. COST, CROX and TWST have explicit synthetic closing references aged 26, 239 and 47 minutes, respectively, within the approved 240-minute planning-only bound. Native targets and actuals retain observed-price five-minute limits. PATH's missing estimates prevent the global chronological cash/share projection; no ending cash, ending holdings or complete trade projection is fabricated.

## Focused actuals repair and recovery

The original attempt completed seven stages, then actuals rejected the prior day's v3 `planning_close` endpoint as if it were legacy `observed_close`. All saved dates, clocks and source identities were correct. The reader now accepts the endpoint name required by the exact saved v3 contract, while preserving legacy behavior and strict observed actual-price selection. Nine regression cases were added; **152 relevant tests passed**, diff checks passed, and independent review found no issues. See [repair validation](C:/dev/ducketz/artifacts/analysis/overnight-20260916/actuals-repair-validation.md).

Native recovery verified the exited owner and failed receipt. The single resume ran only actuals in 17.9 seconds, retained seven completed stages and their pinned Gameplan, and preserved the original deadline with no exception. No training was repeated and prior estimates were not rewritten.

## Provider and operational evidence

Loop A completed all eleven symbols' fundamentals, technicals and signals, with zero blocking or optional capture failures. All 33 OPRA Historical scopes covered September 15, all preflight costs were zero, and there were no failed, deferred, blocked or Live-replay scopes. Current native health selected 66,317 verified partitions; it does not report invalid/skipped retained directories, so this is not a claim that the entire retained archive is pristine. Known FMP clock-skew/CME retained-source advisories and optional stale Pricing-family quarantine were investigated and preserved. See [provider completion](C:/dev/ducketz/artifacts/analysis/overnight-20260916/provider-completion.md).

The prior daytime task's result 1 is the already-handled September 15 16:49 status-file replacement failure. Its focused retry repair and 111-test validation are documented by daytime supervision; this overnight task did not restart that session. Existing schedules remain unchanged. See [daytime audit](C:/dev/ducketz/artifacts/analysis/overnight-20260916/daytime-schedule-audit.md).

## Final verification

The full read-only verifier exited **0** on September 16 at approximately 06:19 UTC: **all eleven checks passed**, with status `VERIFIED_WITH_COVERAGE_NOTES` and no errors. It checked the complete attempt ancestry and log hashes; current Gameplan and source identity; OPRA cursors; XNAS archive files and current acquisition; provider scope logs; enrichment; frozen account, planning and cash/share output; reconstructed saved price paths and synthetic-reference lineage; cumulative evaluation; and recomputed prior-session actuals. The XNAS acquisition downloaded all eleven symbols, failed zero, cost zero and passed capacity checks. Missing observations were preserved rather than filled for model targets or actuals.

September 15 actuals contain 264 outcomes: **159 evaluated, 39 mature awaiting data and 66 pending**. The 154 same-clock price rows contain **110 compared, 24 awaiting eligible observations and 20 without a saved estimate**. Original forecasts, planning estimates and pre-repair hashes are unchanged. Cumulative evaluation covers fifteen original publications with their own saved universes: 2,664 forecasts, 2,116 evaluated, 375 mature awaiting data and 173 pending.

Evidence: [final verification JSON](C:/dev/ducketz/artifacts/analysis/overnight-20260916/final-verification.json), [verification completion](C:/dev/ducketz/artifacts/analysis/overnight-20260916/final-verification-completion.json), [current trade-plan audit](C:/dev/ducketz/artifacts/analysis/overnight-20260916/trade-plan-audit.md), [actuals completion](C:/dev/ducketz/artifacts/analysis/overnight-20260916/actuals-completion.md).

The actuals report scores saved direction against the raw observed price move (90/159 correct, 56.60%). Cumulative evaluation scores thresholded probability against the cost-adjusted target (113/159, 71.07% for September 15). These are different metrics: identical row IDs and returns reconcile exactly, with 25 changed correctness labels producing a net 23-call difference. Neither measures realized broker P/L. See [scoring definitions](C:/dev/ducketz/artifacts/analysis/overnight-20260916/scoring-definition-note.md).

Own supervision UUID `e963374f-1611-4811-9a73-8fbec043bb0b` was released successfully at **2026-09-16T06:21:30.893622Z**. All bounded audit tasks have finished; no renewal helper was started. [Native release receipt](C:/dev/ducketz/artifacts/analysis/overnight-20260916/supervision-release.json). Current run record: **September 16 06:21:31 UTC / September 15 23:21:31 Pacific**. Subsequent healthy wakes should recognize the completed September 15 source / September 16 action run and avoid rerunning it.
