# OPRA final health rebuild progress

Read-only observation completed **2026-09-16 05:24:23 UTC**. The long quiet phase is the native **full OPRA health rebuild**, after current acquisition scopes completed; it is not limited to today's 33 production scope downloads.

Evidence:

- All 33 production cursor JSONs record `completed_through=2026-09-16`, and none retains `replay_coverage`. This is a metadata observation, not an independent replay of partition verification.
- After the scope loop and final cursor reconciliation, `synchronize_option_history` calls `publish_health` once, then prints `REFRESHED_OPRA_HEALTH` only when it returns.
- `publish_health` iterates every default `STANDARD_SCHEMAS` partition in the canonical archive. Each partition verifies manifest/receipt identity, raw and normalized checksums, then validates Parquet content statistics and duplicate natural keys. It writes the new health JSON at the end.
- Direct process evidence at **05:24:21.602010 UTC** caught PID 48416 reading **`ohlcv-1m/TWST.OPT/dates/2022-01-24/segments/full-day/normalized.parquet`**, outside today's three requested schemas and session. This corroborates the full historical health traversal.
- Five lightweight process samples over approximately three seconds showed CPU time rising from **2,483.25 to 2,484.8125 seconds** and read bytes from **234,276,858,458 to 234,282,257,853**. No process disturbance or extra verifier was used.

The prior completed health JSON (September 15 05:15:55 UTC) describes **66,284 partitions across eleven schemas**, **4,811,914,081 rows**, 81,070,340,288 raw bytes and 63,349,863,354 Parquet bytes. Those historical totals provide scale only; they are not a current progress denominator or an ETA. Its older timestamp is expected while the replacement is being built. Completion still requires the native refreshed-health line and maintenance summary.

No new error or stall is established. Keep normal supervision and wait for the native verification outcome. This audit read only source code, 33 small cursor JSONs, the existing health JSON and inexpensive process handles/counters; it did not traverse archive contents, invoke a verifier, make provider calls, claim supervision or modify production state.

Evidence: [process and cursor snapshot](C:/dev/ducketz/artifacts/analysis/overnight-20260916/opra-health-phase.json), [health call](C:/dev/ducketz/datafetching/options_runtime.py:1251), [full inventory traversal](C:/dev/ducketz/datafetching/databento_opra_history.py:903), [partition verification](C:/dev/ducketz/datafetching/databento_opra_history.py:643), [native log](C:/DATASTORE/ml/overnight-runs/20260916T040757.217937Z/loop_a_close_fetch.log).
