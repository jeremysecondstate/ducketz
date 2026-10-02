CME inline history repair verified at 2026-09-30T07:36:23.618601+00:00.

Root owns supervision UUID REDACTED_NATIVE_ID_0120. This delegated repair did not claim/release ownership and did not touch the completed overnight run's bound operator notes.

The inline fetch now sends uncapped original provider rows through the existing CME event-history writer before materialization. This fixes the disconnect between fresh hot captures and the preferred partitioned reader without inventing a second merge/provenance policy. Existing hot/raw evidence is retained. All capped captures stay outside canonical event history with explicit advisories. Context OHLCV/BBO/MBP acquisition, persistence or saturation failures block context publication; contracts-only failures retain evidence and do not block valid context. No controls, trader, model gates, limits, provider acquisition settings or history policies changed.

Validation: 32 CME tests passed, including stale-partition/fresh-inline, identity and timestamp retention, repeated capture, cap safety, four context failure paths and five contracts-only isolation cases. The interpreter emits an unclosed asyncio event-loop ResourceWarning at shutdown after pytest exit0; this is retained in test evidence. Existing native partition writer and context materializer files match their before hashes.

Future runs will use this code. No production materialization, downloads, broker calls, completed-run replay or publication update was performed. Databento's separate Sep30 degraded condition and stale/capped MBP limitations remain explicit.

Evidence: repair-verification.json, repair.diff, source-baseline.json, saved before files and tests logs in this directory. Independent peer review pending at this write.
