# H.Y.P.E.R. UI implementation

Implemented September 25, 2026 in the existing `C:/dev/ducketz` checkout.
H.Y.P.E.R. is mounted immediately after Hyperliquid Duckets. The existing manual
account workspace remains mounted separately.

**Powder update, September 25:** the later
[activation/recovery implementation](../../hyperliquid-system-analysis/POWDER_ACTIVATION.md)
adds a separately launched execution runtime and a read-only projection of its
actual observations, order intents and confirmed fills. Powder remains inactive
until the user runs the explicit command. The original disconnected screenshots
and delivery details below describe the initial UI version. Current offline
examples: [Powder fill evidence](powder-execution-fixture.png) and
[compact scrolled layout](powder-execution-fixture-compact.png). These are labeled
fixtures, not real trades.

## Open the application

From `C:/dev/ducketz` in PowerShell:

```powershell
& .\.venv\Scripts\python.exe -m app.main ui
```

Reopen an existing application window to load the new code, then select
**H.Y.P.E.R. → Paper**. This page reads saved local records automatically about
every five seconds. The refresh button only refreshes the view.

In the initial delivery, **Powder** mirrored the composition, displayed **REAL MONEY · PLANNED / Not
connected**, and contained no execution balances, positions or activity. Its
model forecasts were explicitly a shared preview. That version had no Powder
execution adapter. Both current pages still expose no runtime start, stop,
pause, reseed, policy-edit, order or transfer controls.

## Included behavior

- Navy panels, mint selection, compact Paper/Powder navigation and five summary
  metrics. Alex, Jeremy and Clear Pond retain their short-perpetual,
  long-perpetual and spot roles.
- Equity, P/L and drawdown charts with account and time-range selection. Chart
  hover shows the exact value and UTC observation; normal labels use Pacific
  time. The right-hand panels move below the main workspace at smaller widths,
  with vertical scrolling and horizontal journal scrolling where needed.
- Positions, decisions, fills and transfers with account, asset and model
  qualification filters. Passive inherited spot dust is hidden by default and
  available with **Inherited dust**. Transfers have no asset/model attribution;
  selecting such a filter excludes unattributed records. Position qualification
  is never inferred from a current forecast.
- A persistent selected-record inspector shows skip/hold reasons, allocation,
  requested and executed quantities, unfilled remainder, simulated costs and
  available provenance. **Show saved record** exposes the underlying record.
  Clicking a forecast opens its signal and model provenance.
- Separate data/features, forecast, model polling/publication and paper-cycle
  status. **Operations** displays source timestamps and measured work, queue,
  polling and publication durations. Unmeasured durations remain unavailable.
- A single bounded background reader sends results through a queue. Tk applies
  results on its own thread, preserving filters, chart choices and selected row
  IDs. Destroying the view cancels its callbacks; an in-flight read cannot
  repaint a destroyed view. Failed reads retain the prior view with an explicit
  read-failed state and updated source ages.

## Data and accounting

`app/services/hyperliquid_paper_view.py` uses a `mode=ro` SQLite connection,
`query_only`, a read transaction, a busy timeout and a query time budget. It
does not instantiate `PaperLedger`, invoke runtime commands or use network
clients. SQLite may use its own WAL coordination sidecars when opening a live
database; the adapter does not change authoritative records or configuration.

The committed ledger supplies balances, positions and journals. Headline P/L
uses recorded `total_pnl`, including the opening baseline and transfer
adjustments; it does not use inherited realized P/L. Pooled observations are
selected directly instead of summing both pool and account rows. Account
drawdown uses opening equity plus recorded `total_pnl` so internal cash
transfers do not become artificial gains or losses.

The adapter reads the latest 500 records per journal by default. Equity history
covers the full period with a bounded last-observation-per-bucket projection
when necessary; drawdown peaks are computed before sampling. The chart can
further sample for display and never sums valuation observations. Missing or
incompatible artifacts produce visible partial/missing/error states, not zero
balances. The performance export retains its independent timestamp and may
lag the current ledger.

## Verification

The following relevant suites passed: **81 tests** total (64 adapter,
integration and existing manual-workspace checks; 17 new workspace checks).

```powershell
& .\.venv\Scripts\python.exe -m pytest tests/test_ducket_bucket_app.py tests/test_hyperliquid_duckets_ui.py tests/test_hyperliquid_workspace_services.py tests/test_hyperliquid_paper_view.py tests/test_hyper_workspace.py -q
```

Tests cover accounting, transfers, sampling, missing/stale/partial/corrupt
artifacts, mismatched observation cycles, forecast validation, filtering,
partial fills, selection persistence, failed reads, bounded threading,
destruction and empty Powder execution state. UI tests block network calls,
process creation and ledger construction.

Both modes were rendered and inspected at 1706×1000 and 1180×760, including
scrolled compact views. Exposure meters and chart labels were corrected after
visual inspection. A separate read-only snapshot of current local artifacts
rendered successfully; a representative adapter read took approximately 53 ms.
The three existing runtimes were observed running. No worker lifecycle or
strategy setting was changed by this implementation.

| View | Screenshot |
| --- | --- |
| Paper, illustrative offline fixture | [1706×1000](implementation-paper-1706.png) |
| Powder, shared forecast fixture and empty execution | [1706×1000](implementation-powder-1706.png) |
| Paper, compact | [Top](implementation-paper-1180.png) · [Scrolled](implementation-paper-1180-bottom.png) |
| Powder, compact | [Top](implementation-powder-1180.png) · [Scrolled](implementation-powder-1180-bottom.png) |
| Paper, actual local ledger observation | [Read-only local snapshot](implementation-paper-local.png) |

The fixture is separate from the production tab and labels all illustrative
values. Run or capture it with:

```powershell
& .\.venv\Scripts\python.exe tests/visual_hyper_workspace_fixture.py --mode Paper --size 1706x1000
& .\.venv\Scripts\python.exe tests/visual_hyper_workspace_fixture.py --mode Powder --size 1180x760 --scroll-bottom --capture artifacts/validation/hyper-powder-compact.png
```

To preview one current local read without launching any other application tabs:

```powershell
& .\.venv\Scripts\python.exe tests/visual_hyper_workspace_fixture.py --local-data-root C:/DATASTORE/hyperliquid --view Decisions --size 1706x1000
```

This option loads once before constructing Tk, then displays the resulting
in-memory snapshot with a **CURRENT LOCAL RECORDS · read-only** footer. The
production application's H.Y.P.E.R. tab performs the ongoing five-second reads.

## Files

- `app/ui/hyper_workspace.py`: workspace, chart, filters, inspector and lifecycle.
- `app/services/hyperliquid_paper_view.py`: read-only artifact projection.
- `app/ui/ducket_bucket.py`: application tab mount.
- `tests/test_hyper_workspace.py`, `tests/test_hyperliquid_paper_view.py` and
  `tests/test_ducket_bucket_app.py`: verification.
- `tests/visual_hyper_workspace_fixture.py`: offline and read-only local preview.

The original handoff and all earlier project work were preserved. Unrelated
files produced concurrently elsewhere in the checkout were left untouched.
