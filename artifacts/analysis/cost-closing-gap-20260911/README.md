# COST closing-window retries — September 10, 2026

## Approved follow-up: bounded planning completion

After these retries, the user explicitly authorized carrying forward the last actual price in rare no-trade gaps. The implemented policy fills only a current planning reference gap greater than five and at most fifteen minutes, within the exact previous exchange session and a completely verified source interval. It generates separate one-minute OHLC=last-close, volume=0, `is_synthetic=true` rows with `ASSUMED_NO_TRADES` provenance. Original observed timestamps, native data, historical samples, actuals, and independent quote freshness remain intact.

The new implementation is in `ml/gameplan_price_completion.py` and the v4 trade-planning publisher. All184 focused tests pass. `forward-fill-reproduction.json` verifies COST's thirteen synthetic rows at902.20 and restores all98planning points/168augmented rows using saved evidence. Native resume20260911T061454.689692Z executes only planning and actuals, retaining allsixcompleted upstream stages and the original11:00UTCdeadline. See that run's final receipt and operator verification for publication status.

The previous dual-API requirement and decision to leave fallback inactive below describe the earlier retry phase. They are superseded for this bounded synthetic planning policy; historical retry evidence itself is unchanged.

## Earlier retry evidence

The issue is a missing sufficiently recent observed closing price for the 17:00 Pacific boundary. Input candles are **one minute**; five minutes is the allowed boundary distance. XNAS's final candle starts16:46 and completes16:47, leaving13minutes to the boundary.

All requests were COST only, September10 16:30–17:00 Pacific (23:30–00:00UTC). Historical Databento exact-cost preflights were$0; no subscriptions or licenses changed.

| Source | Historical result | Live result | Sufficient closing observation? |
| --- | --- | --- | --- |
| Databento XNAS.ITCH | Three candles, latest completes16:47 | Access denied | No |
| Databento EQUS.MINI | Successfully empty | Successfully empty replay | No |
| Schwab minute price history | Three scoped candles, latest completes16:51 | Not requested | No |

The supplied Standard-plan live screenshot explicitly lists Databento US Equities Mini (EQUS.MINI), not XNAS.ITCH. EQUS.MINI Live was accessible and completed. Its successful empty response does not establish what XNAS Live would return. Empty minute intervals may contain no qualifying trades on that feed; no synthetic candles were created.

During the retry phase, the user required both Databento APIs to confirm the gap before allowing fallback. XNAS's access denial did not count as confirmation. Independently, Schwab had no candle within five minutes of the required boundary. No source substitution, age-tolerance change, production publication, pipeline resume, or broker order occurred during that phase, which ended with the planning tail blocked.

`verified-results.json` is the authoritative offline audit. The initial EQUS diagnostic's `comparison.json` records local XNAS-validator rejections after successful provider delivery; `verify_probes.py` separately verifies the actual EQUS metadata, COST mapping, every requested minute interval marker, and replay completion. Raw responses are preserved. The EQUS native audit was independently reproduced by a second agent.

A pure fallback design draft, if present in `draft/`, is inactive and not imported by production code. It must not be described as an implemented automatic fallback. Production integration is deferred under the user's confirmation condition.
