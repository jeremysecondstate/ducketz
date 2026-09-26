# Fresh CME acquisition and optional context exclusion

Native Databento/watchlist collection finished successfully at 2026-09-25 04:24:41 UTC for all eleven configured stocks. All six bounded CME capture scopes contain their configured symbols: five continuous roots (ES, NQ, RTY, CL and GC), or four literal contracts (ESZ6, NQZ6, CLX6 and GCZ6). Symbol membership matches the prior completed capture audit. The first continuous OHLCV request received a gateway 504; native retry two succeeded. There is no unresolved acquisition error in that request sequence.

| Scope | Schema | Current captured rows | Latest actual event UTC | Limit saturated |
|---|---|---:|---|---|
| Continuous | OHLCV 1m | 2,372 | September 25 03:59 | No |
| Continuous | BBO 1m | 300 | September 25 04:08:58.798 | No |
| Continuous | MBP 10 | 5,000 | September 24 20:16:23.519 | Yes |
| Raw contracts | OHLCV 1m | 4,078 | September 25 03:59 | No |
| Raw contracts | BBO 1m | 240 | September 25 04:08:58.798 | No |
| Raw contracts | MBP 10 | 5,000 | September 24 20:16:25.755 | Yes |

These are the native bounded captures, not proof of every record in the initial three-day OHLCV request. Native effective OHLCV ranges shrank to September 24 19:00–September 25 04:00 UTC for continuous roots and September 24 10:00–September 25 04:00 UTC for raw contracts. Each MBP request shrank five times to a 46.875-second interval and still hit the 5,000-record cap. All actual event timestamps precede their receipt timestamps; all receipts precede the recorded advisory.

The fresh optional cross-asset diagnostic still rejects the September 3 21:00 UTC common hour: NQ BBO is stale by **21 days 07:24:42**, exceeding the 15-minute gate. This is the prior saved-window limitation advancing by one day, not a claim that today's collection lacks the requested symbols. The causes remain:

1. `datafetching/cme_cross_asset_context.py` selects retained partitioned event history whenever any exists. Its newest OHLCV/BBO partitions still end September 4 at about 05:09 UTC; MBP ends September 3 at 20:54:59 UTC. The newest exact common OHLCV hour still ends September 3 at 21:00. Inline acquisition writes separate aggregate pool files and does not advance those selected partitions.
2. The fresh continuous aggregates carry actual roots in `symbol`, but the group alias `CME_CONTEXT` in `provider_symbol`. The reader prefers `provider_symbol`, so a pure calculation on the unmodified current aggregates identifies no continuous one-minute rows. Literal raw contracts remain separately excluded by their intended `raw_symbol` type.
3. A diagnostic-only calculation using the actual continuous `symbol` field immediately rejects current MBP limit saturation. The newest MBP observation is approximately 8 hours 8 minutes old; the newest BBO is 15 minutes 42 seconds old at advisory time, also beyond the 15-minute gate. Fixing a selector or identity field alone therefore would not qualify this current context.

Successful collection and strict optional-feature eligibility remain separate. No provider calls, source substitutions, configuration/code changes, relaxed gates, synthetic data or production model-qualification claims were made. The bounded audit, exact paths/hashes, prior baseline comparison, source timestamps and pure diagnostic results are saved in `cme-advisory-review.json`.
