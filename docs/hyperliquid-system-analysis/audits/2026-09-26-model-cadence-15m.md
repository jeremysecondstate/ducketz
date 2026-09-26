# Model cadence changed to 15 minutes

User requested fitting on each completed 15-minute candle. Before the change,
the latest four candidate reports recorded 1.31–1.35 seconds each (5.32 seconds
combined); the five-second scheduler adds collection/publication delay.
The former hourly cadence was not a compute-capacity limit.

## Change and verification

- Before check: **2026-09-26T09:09:07.860489Z**. Model PID 60964 accepted
  `retrain_seconds=3600`. Requested its normal graceful stop and verified the
  process exited, lifetime lock released and stop marker consumed.
- Changed `configs/hyperliquid-models.json` to `retrain_seconds=900`. Relaunched
  the normal model module through hidden `Win32_Process.Create`; no force-fit
  option, journal reset or model-artifact deletion was used.
- At **09:11:07 UTC**, new model PID **37840** accepted 900 seconds, with no
  configuration or market errors. Its venv launcher **53420** and cmd wrapper
  **2812** were outside Windows jobs; process creation times were coherent.
  The actual worker has its own launcher job, as in the earlier recovery.
- Data **57004** and Paper **73216** retained their identities. Paper's committed
  observation advanced from **09:08:58.239261** to **09:11:06.313680 UTC**.
- Paper seed **07:42:57.850335836 UTC**, pooled opening equity
  **42071.69075976541**, policy **60b7f962240a0857** and qualified-only inclusion
  remained unchanged. No Powder action was taken.

The first new candidates used the completed **09:00 UTC** source candle:

| Market | Published UTC | Qualified | Brier | Log loss |
| --- | --- | --- | ---: | ---: |
| BTC | 09:09:54 | No | 0.253234 | 0.699630 |
| ETH | 09:09:59 | Yes | 0.243263 | 0.679514 |
| HYPE | 09:10:04 | Yes | 0.249697 | 0.692500 |
| ZEC | 09:10:09 | Yes | 0.245969 | 0.685019 |

These are candidate assessment results, not rewritten forecasts or guaranteed
fills. The existing 09:00 forecasts retain their original model IDs; newly
qualified models can be selected for subsequent candle forecasts. A candidate
passing today does not establish that increased cadence caused better skill.

## Scope and limitations

The qualification checks, 192-row calibration, 288-row assessment, four-bar
outcome horizon and observed 123-hour fitting-data lag were not changed.
Adjacent scheduled assessments normally share 287 of 288 rows. More frequent
checks provide earlier updates, not independent attempts at proving an edge.
Treat this timestamp as a schedule change when comparing Paper performance.

**215 tests passed**: 130 model-runtime/artifact/view/workspace cases, then 85
model-configuration cases. Existing runtime coverage explicitly checks 900 vs
3600 seconds using completed-candle progress. Added five view cases for accepted
cadence and legacy/invalid-status fallback. Live verification confirms accepted
cadence and first new publications; it does not claim a long-run uptime or
profitability result.

Machine-readable evidence:
`C:/DATASTORE/hyperliquid/_operations/model-cadence-15m-verification.json`.
