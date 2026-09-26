# Current OPRA maintenance progress

Snapshot **2026-09-26 04:37:07 UTC** is bound to Loop A cycle `20260926T040724.729021Z-pid46624` and native run `20260926T040723.834511Z`. The base datastore fetch completed at 04:33:55 with zero failures. Its following OPRA maintenance remains in progress; this is not native Loop A stage completion.

There are **15/33** fresh exact preflights: all eleven `ohlcv-1h` scopes and four `cbbo-1m` scopes. Eighteen scopes remain pending. Every observed scope passes semantic checksum, configured symbol/schema identity, nested request equality, current provider/included-range bounds, finite nonnegative record/size counts, exact zero-dollar cost, and the `5 GiB + 2 × estimated bytes` capacity rule. Observed download estimates total **2,372,295,256 bytes**, within the native 20,000,000,000-byte selection ceiling; this is a partial total.

Current entitlement metadata was observed at **04:33:57 UTC**. All three production schemas are available through the required exclusive **September 26 00:00 UTC** boundary, covering September 25. **0/33** production symbol/schema cursors have reached that boundary, and no September 25 full-day partition receipt exists yet in this snapshot. Metadata approval is not acquisition completion.

The native synchronizer produces preflights sequentially and prints its aggregate guarded-preflight summary only after collecting the scopes. No separate current 33-scope progress JSON is published by this path. `state/status.json` is retained schema state and should not be used as current maintenance progress.

Read current progress from:

- `C:/DATASTORE/ml/overnight-runs/20260926T040723.834511Z/loop_a_close_fetch.log`
- `C:/DATASTORE/market-data/databento/opra/OPRA.PILLAR/metadata/entitlement.json`
- `C:/DATASTORE/market-data/databento/opra/OPRA.PILLAR/metadata/preflights/{schema}/{symbol}.OPT/*_to_2026-09-26/preflight.json`
- `C:/DATASTORE/market-data/databento/opra/OPRA.PILLAR/state/symbol-history/{symbol}/{schema}.json`

The bounded reusable audit is `snapshot_opra_progress.py`. It writes timestamped evidence and `opra-progress-latest.json` only in this preflight directory. It performs no provider/broker calls, native writes, archive payload scans or supervision operations. The native CLI displays its legacy one-dollar budget, but all observed exact requests were zero dollars; preserve that actual-cost check as the remaining scopes appear.
