# September 25 preparation preflight

Local evidence was freshly read on September 25 UTC for September 24's completed source session. No provider/broker calls, production writes, trader starts, order actions or claim operations were performed by this preflight.

The current production watchlist contains eleven symbols: AAPL, AMZN, GOOG, MU, NVDA, SNDK, COST, CROX, PATH, TWST and IONQ. Expected publication counts are 264 forecasts, 264 intents and 33 production OPRA cursors. The registered CROX/PATH/TWST/IONQ batch remains ACTIVE with COMPLETE progress. Its current watchlist membership and saved Gameplan, trade-plan and overnight receipt hashes verify. No unfinished registered batch was found.

The September 24 manual Gameplan trader is terminal FINISHED with a 00:00 UTC heartbeat and no matching trader process. Its final decision receipt/manifest/decisions hashes verify; the final cycle selected/submitted zero orders and recorded ready inventory reconciliation. The session recorded 102 daytime submissions and 62 failed cycles; this bounded preflight does not diagnose those earlier cycles.

The latest saved ledger snapshot is ready at 2026-09-24T23:59:56.524535+00:00 with no persistent blocks. There are 346 FILLED, 12 CANCELLED, three WORKING and one PARTIAL reservations. Native read-only ownership validation against that saved snapshot returns REVIEW_REQUIRED because four reservations still need reconciliation:

| Symbol | Horizon | Side | Order shares | Confirmed filled | Still reserved |
|---|---|---|---:|---:|---:|
| CROX | 1h | SELL | 51 | 0 | 51 |
| CROX | 1h | BUY | 16 | 0 | 16 |
| CROX | 4h | BUY | 32 | 0 | 32 |
| TWST | 1h | BUY | 10 | 4 | 6 |

The current broker states remain unknown. Do not infer cancellation from the closed session or absence of a trader. Root should use the documented native reconciliation path before planning: exact terminal order evidence from the matching account, followed by a newer coherent holdings snapshot, under the session and cycle locks. Existing actual fills, assigned inventory and entry claims remain protected.

The selected nonsecret CME configuration uses continuous ES.v.0/NQ.v.0/RTY.v.0/CL.v.0/GC.v.0 and literal raw ESZ6/NQZ6/CLX6/GCZ6. All four raw instrument IDs match their corresponding continuous IDs in the latest saved BBO captures, which end on September 24 at approximately 04:38 UTC. The retained GCZ6 native definition receipt, raw and normalized bytes verify; its saved expiry is December 29, 2026. These historical captures do not establish tonight's fresh acquisition coverage or a future roll. No configuration change is indicated by this evidence.

Detailed source hashes, holdings, allocation windows and CME evidence are in `local-preflight.json`. The source-bound root-review helper is `native_reconcile.py`; `reconciliation-plan.json` binds the current ledger and 57 terminal/control/claim files. Compile and `--validate-only` passed without broker calls. The helper exposes only native broker reads, permits complete terminal evidence with preserved cumulative fills, rehearses native reconciliation on a ledger copy and verifies exact fill/assignment deltas before the one zero-budget production reconcile. Root must review and execute `--apply`; this preflight did not execute it.
