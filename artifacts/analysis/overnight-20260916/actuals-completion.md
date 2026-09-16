# September 15 actuals: completed after scoped recovery

The native actuals-only resume completed at **2026-09-16T06:15:09.431187+00:00**, exit 0, with the original September 16 04:00 Pacific deadline and pinned successor preserved. The new [readable results](C:/DATASTORE/ml/gameplan-actuals-review-runs/20260916T061453.637702Z/Gameplan-results.md) review September 15 through its 17:00 Pacific close. Overnight orders remain zero.

The prior saved planning path, trade-plan manifest and source receipt match the pre-repair diagnosis hashes. Both prior and successor forecast files match the earlier weekly-diagnosis hashes. No saved estimate, direction or forecast identity changed. New receipt/manifest/output hashes, exact prior/successor input bindings, latest pointer, dated reader and successor results link verified.

- Forecasts: **264**, with **159 evaluated**, **39 mature awaiting data**, and **66 pending**.
- Direction: **90/159 correct (56.60%)** among mature, observed bullish/bearish calls. Neutral, pending and missing outcomes remain excluded.
- Same-clock prices: **154**, status counts **{'COMPARED': 110, 'MATURE_AWAITING_DATA': 24, 'NO_SAVED_ESTIMATE': 20}**; **19** compared prices inside their original saved range.
- Saved low/mid/high estimates match every original point exactly. All present same-clock actuals retain the correct observation side and native five-minute bound. Unavailable estimates and missing actuals retain null error/range comparisons. The v3 planning-close compatibility repair supplies no actual prices.

The boundary diagnostics classify missing same-clock observations as {'OUTSIDE_TOLERANCE': 21, 'NO_OBSERVATION': 3}; source-window coverage is {'VERIFIED_COMPLETE': 24}. Complete requested coverage does not establish a trade occurred or permit a synthetic actual. Detailed 17:00 per-symbol coverage and checksums are in [the bounded audit JSON](C:/dev/ducketz/artifacts/analysis/overnight-20260916/actuals-completion.json).

This audit reads only saved small artifacts and checksums; it does not reload the native price archive or duplicate the supervisor's full actuals recomputation. Market price direction accuracy is not broker-fill evidence or realized P/L. No production code, process, claims or publications were changed by this audit.
