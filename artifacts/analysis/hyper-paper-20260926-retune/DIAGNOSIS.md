# Paper run diagnosis and bounded adjustment

Analyzed only eligible experiment `20260926T123436Z-book-vwap`, using a read-only
SQLite transaction and the committed **2026-09-26 15:40:47 UTC** observation
(08:40:47 PT). No archived or excluded experiment supplies these results.

## Matched accounting

| Measure | USD |
| --- | ---: |
| Opening equity, 12:34:36 UTC | 42,088.24 |
| Paper equity at selected observation | 42,010.08 |
| Paper P/L including fees and estimated funding | -78.17 |
| Cumulative simulated fees | 42.59 |
| Estimated funding received | 0.24 |
| Paper P/L before fees and funding | -35.82 |
| Opening inventory held at the same marks, excluding funding | -2.79 |
| Paper less opening-hold return, both excluding funding | -75.62 |

Residual spot quantities preserve a recorded mark for each opening inventory
market. The comparison values the unchanged opening positions using those
same-observation marks. It does **not** use a different-time real-account UI
total. It is an opening-hold counterfactual, not a reconstruction of subsequent
real trades, transfers, deposits, or withdrawals. Hypothetical holding funding
is unavailable and is excluded from both sides of the comparison.

The per-market identity checked to tolerance 1e-7 USD is:

`paper P/L - funding - opening-hold P/L = sum(fill quantity * (latest mark - fill price) - fill fee)`.

## Startup and subsequent costs

The first trading sweep at 12:35:21 UTC made **seven fills** with **$47,880.58**
turnover and **$26.91** in fees, 63.2% of the total fee bill. Execution prices
added **$2.11** of cost relative to opening marks; that amount combines book
spread/impact and movement between the opening and first execution snapshot.
Extra simulated slippage was zero.

Opening gross exposure was **$51,023.26** against $42,088.24 equity. The current
15% per-symbol and 60% pool caps intentionally transform that inherited
portfolio. In particular, Clear Pond's BTC was liquidated for $16,834.94
notional and $11.78 fees while the BTC forecast was Research: it was a risk-cap
reduction, not a Research-driven directional trade. The HYPE and ZEC opening
adjustments closed opposing inventory or resized to accepted forecast targets.
A new 1:1 mirror will again incur transition costs if inherited positions and
the strategy's caps/targets differ.

The remaining **31 fills** account for **$34,845.70** turnover and **$15.68** in
fees. No stop-loss fills occurred. Most ongoing exposure was short ETH/HYPE/ZEC;
those positions and adjustments lost value during the later price rebound.
The run spans several policy phases and about three hours, so it cannot isolate
model-training quality or establish a profitable replacement setting.

## Adjustment selected for a fresh forward run

Changed only the checked-in Paper `rebalance_min_delta_fraction` from **0.10 to
0.20**. Ordinary changes now require at least the larger of $25 or **20% of the
target notional**. Shared 49%/51% signal thresholds, qualified-only forecasts,
fees, risk caps and other settings remain as they were. The constructor default
remains 0.10 for compatibility; the runtime JSON explicitly selects 0.20.

Screening recorded post-opening decisions with their original current/target
notionals finds **seven** ordinary fills that fall below the 20% threshold:
$3,202.48 turnover and $1.44 direct fees. A 25% threshold identifies the same
seven, so the smaller adjustment was selected. These figures are **not a
sequential replay or predicted savings**: deferring a fill changes later
inventory, collateral, fees and subsequent decisions. This is a modest turnover
control for forward evaluation, not a fix for all observed underperformance.

Complete exits and forced risk reductions still bypass the ordinary rebalance
threshold. Existing order-book quantity precision and executable fill minimums
still apply. Runtime restart is required to load the changed configuration;
this diagnosis did not restart a process or alter a ledger.

## Evidence and verification

- `matched-mark-evidence.json` includes the frozen seed, committed observation,
  marks, fill rows, per-market attribution, limitations and source cycle hash.
- `current-run-diagnosis.json` contains the original compact snapshot and
  decision reason counts.
- `diagnose_current_run.py` reproduces matched accounting using only the
  explicitly supplied eligible ledger and an optional exact observation time.
- Command: `.venv/Scripts/python.exe -m pytest tests/test_hyperliquid_paper_policy.py tests/test_hyperliquid_paper_runtime.py -q`
- Result: **231 passed in 12.24s**. The existing rebalance test now covers both
  10% and 20%, a representative short trim deferred at 20%, a larger adjustment,
  minimum notional behavior, complete long/short exits, and forced reductions.

The matched snapshot can be reproduced before archival with:

```powershell
.\.venv\Scripts\python.exe artifacts\analysis\hyper-paper-20260926-retune\diagnose_current_run.py --paper-root C:\DATASTORE\hyperliquid\_paper --as-of 2026-09-26T15:40:47.011237+00:00 --output report.json
```

After an authorized archive, supply that preserved eligible experiment's Paper
directory explicitly. Do not use the script to discover or evaluate excluded
experiments. The bundled frozen evidence remains the reference for this report.
