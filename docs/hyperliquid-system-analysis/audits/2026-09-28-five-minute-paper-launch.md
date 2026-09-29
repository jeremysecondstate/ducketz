# Five-minute Paper launch — September 28, 2026

The user authorized implementation after the isolated feasibility review, including an early archive and a fresh actual-account mirror. Experiment **20260928-paper-5m-01** is running. Maintenance completed at **23:21:14 UTC / 16:21:14 PDT**.

## Deployed behavior

- BTC, ETH, HYPE and ZEC use **5m OHLCV → next-5m forecasts (h1)**. Full fitting is due every 300 seconds of source-candle progress; model polling remains 5 seconds and Paper book/risk polling remains 30 seconds.
- The coordinator onboarded 5,000 candles per market, then overlapped/appended the next completed candle to 5,001 rows. Repair is every 288 cycles, retaining the prior daily repair frequency. History remains cumulative across Paper resets.
- Logistic / ExtraTrees / HistGradientBoosting / MLP remain present with positive **40/20/20/20** weights and calibration C=0.1. The 70/15/15 split, purging, qualification, fees, stops and exposure limits remain unchanged. No model family was removed.
- Forecast maximum age is 300 seconds; target maturity also rejects expired forecasts. Fixed-bar features now cover shorter clock windows and the existing horizon-based stop cooldown becomes five minutes. Forecast expiry does not require closing positions.
- Dashboard and shared Powder preview resolve the configured recipe. Health binds actual configured slots and model settings to fresh forecasts, so old 15m artifacts cannot satisfy a 5m check. Research supports configured intervals/horizons and preserves calibration/weights in its split controls.
- Missing-forecast decision buckets follow the candle interval. The separate 900-second performance-export cadence is unchanged.

Five-minute history is preserved for future aggregation. No 10m/15m resampler or simultaneous multi-interval collection was added. The improvement contract requires retaining continuous 5m collection if a future longer-bar trial is implemented.

## Archive and fresh opening

The prior short 15m experiment closed with Paper equity **$43,898.95** versus actual Duckets **$43,935.73**. Its common-mark gap was **-$41.36**, with external flows verified empty. This brief run is preserved; it is not enough evidence to rank the two frequencies.

The complete old Paper/model trees were gracefully stopped and archived: **142 files / 78,477,418 bytes**, SHA-256 verified. A separate checked copy preserves all four 15m datasets and coordinator history: **5,680 files / 2,767,515,190 bytes**. Original 15m datasets remain in place as well.

Archive: [20260928-paper-5m-01](C:/DATASTORE/hyperliquid/_paper_archives/20260928-paper-5m-01).

The new mirror was seeded at **2026-09-28 23:17:05 UTC / 16:17:05 PDT**:

| Account | Opening equity |
|---|---:|
| Alex | $6,620.00 |
| Jeremy | $5,607.98 |
| Clear Pond | $31,709.04 |
| **Total** | **$43,937.02** |

Exact total: **43937.0212871266**. All nine inherited signed positions, cash/collateral and historical perpetual entries reconciled to retained public account responses. Opening P/L, fills, fees, funding and virtual transfers were zero. Sequential account/mark reads explain a net $0.00197 source-to-opening difference; no balancing cash was invented. Independent verification also replayed subsequent fills and fees against the same immutable opening.

Seven initial strategy/risk fills cost **$27.6595118545** after the opening. By the final health observation, eight fills cost **$28.26012667**; Paper equity was **$43,908.8020555738** at 23:20:58 UTC. These post-opening costs are retained, not erased by another reset. The trial has not demonstrated outperformance.

## Live verification and regression checks

The first native append closed at **23:20 UTC** and forecasts target **23:25 UTC**. All forecasts were published by **23:20:11.001 UTC** and all four retrained bundles by **23:20:26.375 UTC**. Forecasting used already-published bundles while independent retraining completed, as designed. Paper consumed this new forecast cohort; its committed observations advanced beyond the opening. Final health passed without warnings.

Verified independent workers: data **36524**, models **75320**, Paper **76188**, with exact module/config/root/cwd and process ancestry checks. Powder remains off. Initial and next-cohort qualification: BTC, ETH and ZEC Qualified; HYPE Research and excluded from signal-driven trades. Qualification can change on later fits.

Selected regression groups passed: dashboard/Powder projection/Tk workspace **93**; interval-aware research and adjacent model/classical/sequence checks **169 passed, 1 skipped**; lifecycle/health **24 passed, 1 skipped**; Paper runtime/forecast/coordinator-config **169**; model configuration **110**. These groups overlap and are not a unique total. Additional model runtime/artifact, policy, seed, ledger, comparison, data pipeline/coordinator/loop checks passed; old checked-in-default assertions were updated for the authorized frequency. `git diff --check` passed.

The MLP retains its 100-iteration convergence warnings. The 70/15/15 policy still reserves recent data for calibration/assessment, leaving the base fit about five days behind at onboarding. This trial changes frequency, not that evaluation policy. A small latency sample does not guarantee future tail latency as history grows.

## Scheduled follow-up and evidence

**Hyperliquid Paper Improvement** remains ACTIVE, every three days at 14:00 Pacific, **GPT-6 Astra / Ultra**. Next expected review: **October 1, 2026 at 14:00 PDT**. It compares fresh real-account results after costs, preserves failed trials, archives the run and establishes the next verified mirror. **Hyperliquid Operations Watch** remains ACTIVE every 30 minutes, GPT-6 Luna / Extra High; v13 recognizes the new interval and completed handoff. Both schedules, model settings and projects were preserved while prompts/memory were updated. Local runs require the PC and desktop app available.

Evidence directory: [20260928-paper-5m-01](C:/DATASTORE/hyperliquid/_operations/paper-improvement/20260928-paper-5m-01). Key receipts: `closing-comparison.json`, `preservation-verified.json`, `frequency-preservation-verified.json`, `trial-recipe.json`, `opening-public-account-reads.json`, `opening-verification.json`, `opening.json`, `prepared-health.json`, `first-cycle.json`, `trading-health.json`, `first-5m-update.json`, `advanced-cycle.json`, `final-health.json`, `deployment.json`, and `automation-handoff.json`.

Current durable baseline: [paper-current-accepted.json](C:/DATASTORE/hyperliquid/_operations/paper-current-accepted.json). Historical archives are never automatic recovery targets.
