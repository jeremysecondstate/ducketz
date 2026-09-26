# Handoff prompt: investigate and correct the Paper restart

Work in `C:/dev/ducketz` on Windows / PowerShell, using `.venv/Scripts/python.exe`. The Hyperliquid datastore is `C:/DATASTORE/hyperliquid`. This continues an existing H.Y.P.E.R. Paper trading implementation. Preserve the many unrelated uncommitted changes. Read this handoff and the relevant source before making changes.

## What I want

I asked for a fresh Paper run starting **1:1 with my actual Hyperliquid accounts**, including their balances and existing positions. After reopening the UI, Paper was already below the actual accounts and almost entirely in cash. The previous chat described the restart as verified, but the visible result did not meet my expectation. Investigate this discrepancy and correct the restart behavior. Do not repeat the same reset and call it fixed without explaining and verifying what happens before and after the first trading cycle.

I want a useful Paper experiment with enough executed trades to evaluate what works. We deliberately broadened the entry thresholds, while keeping **Qualified-only** model signals. Do not reinstate the old tighter thresholds or introduce extra restrictions simply to reduce activity. Research and Unavailable forecasts must not initiate ordinary signal trades. Explain necessary risk actions separately from signal trades.

Start with a read-only reconstruction of the opening and earliest cycles. Distinguish an incorrect seed, immediate strategy/risk liquidation, trading costs, market movement, and UI valuation/export timing. Then implement the appropriate fix and test it. Before another reset, establish the intended treatment of inherited positions and provide a concrete, reviewable explanation of the changed behavior. The latest request was to prepare this handoff; the previous chat made no operational changes for it.

## Latest screenshots and the unresolved problem

Both screenshots are from September 26, 2026, Pacific time:

| View | Displayed observation | Pool equity | Other evidence |
| --- | --- | ---: | --- |
| Actual Hyperliquid accounts | Last synced 04:41:32 PT | $42,102.15 | Existing HYPE/ZEC perpetual positions and Clearpond spot holdings remain |
| H.Y.P.E.R. Paper | Portfolio 04:41:08 PT | $42,058.63 | P/L since opening -$40.90; fees $28.27; gross exposure $0.94; essentially only spot dust remains |

I described the starting gap as approximately $60. The exact displayed difference in these two screenshots is **$43.52**, with observations 24 seconds apart. Preserve that distinction without dismissing the underlying complaint. The Paper chart drops immediately at its left edge and then stays nearly flat. Paper also displays a warning that its point-in-time performance export is stale, with an export time of 04:30:32 PT. Explain which panels read the ledger versus an export.

Screenshot files:

- Actual: `C:/Users/7980X/AppData/Local/Temp/codex-clipboard-4e553eec-11ca-42f6-a91e-0c716a0819aa.png`
- Paper: `C:/Users/7980X/AppData/Local/Temp/codex-clipboard-3e18ac24-7bc8-46c9-832a-fe597d966f25.png`

Reopening the UI does not create a new Paper seed. The screenshot is about eleven minutes after the recorded seed. This timing alone does not explain or resolve the immediate drop and flattening.

## Recorded opening: evidence to verify, not a settled conclusion

Current experiment metadata at handoff preparation:

- Experiment: `20260926T113032Z-fresh-entry-4pp`.
- Seed: `2026-09-26T11:30:32.118497133+00:00` = **04:30:32 PT**.
- Opening equity: **$42,099.529976985854**, nine inherited positions including dust.
- Alex: $5,879.21164292961; Jeremy: $6,151.792708243839; Clear Pond: $30,068.525625812406.
- Metadata: `C:/DATASTORE/hyperliquid/_paper/experiment.json`.
- Earlier verification: `C:/DATASTORE/hyperliquid/_operations/fresh-entry-4pp-verification.json`.
- Earlier audit: `C:/dev/ducketz/docs/hyperliquid-system-analysis/audits/2026-09-26-fresh-entry-4pp-paper.md`.

That audit reports that the opening inventory reconciled to equity and differed from sequential public account summaries by less than nine cents per account. It also reports **seven initial fills**, including inherited-position stop reductions and an exposure-cap adjustment. This establishes why a seed-only check is insufficient; it does not establish that the user-visible restart behavior is correct. Reconstruct the current run independently.

The metadata still says `analysis_eligible: true`. The user now disputes this restart. Do not silently treat it as an accepted comparison baseline or change/delete its evidence merely because of this handoff.

## Investigation and acceptance criteria

1. Identify the active seed and workers afresh. Separate UI reopen, worker resume, and explicit fresh mirror creation. Process IDs below are historical observations, not authority to stop a process.
2. Reconcile each account's source balances, signed quantities, spot holdings, perpetual equity, cash/collateral and opening marks. Document account-read timestamps and valuation differences; sequential exchange reads are not an atomic portfolio snapshot.
3. Reconstruct the first committed cycles, decisions, orders and fills. Attribute the opening-to-current equity change to fees, slippage, funding, price movement and any accounting error. Do not infer this from the screenshots alone.
4. Check how retained historical entry prices affect the 3% stop immediately after seeding. Check exposure caps, account roles, neutral/opposite Qualified signals, forecast availability and cooldown creation. Explain why meaningful holdings became approximately $0.94 of exposure.
5. Resolve the distinction between historical entry/cost-basis display and the risk reference appropriate for a new experiment. Do not silently rebase all entries, remove risk controls, invent cash adjustments, or suppress costs to manufacture equal totals. If a policy choice remains necessary, present the specific tradeoff after investigating it.
6. Make the opening baseline visible and auditable **before any strategy fills**, with $0 experiment P/L and no synthetic seeding fees. Preserve real subsequent costs. A mirror can later diverge through actual simulated decisions, but every immediate adjustment must be explainable and consistent with the agreed restart semantics.
7. Test the relevant seed/accounting/runtime/UI behavior and verify the first cycles after any eventual corrected reset. Do not declare success solely because the process runs, the initial inventory count is nine, or a metadata file says "mirror". Update the operational docs and watch baseline if lifecycle/policy changes actually occur.

## Current configuration and implementation context

Read the files to confirm these settings still apply:

- `configs/hyperliquid-paper.json`: `seed_mode=mirror`, Qualified-only, 30-second cycles; `entry_band=0.04` (new long >=54%, short <=46% P(not-down)); `exit_band=0.02`; saturation 0.15; stop loss 0.03; normal rebalance minimum $25 and 10%; simulated venue minimum $10 plus quantity precision; per-symbol gross fraction 0.15, pool 0.60, account utilization 0.80; perp fee 0.00045, spot fee 0.0007, slippage 2 bps. The PaperConfig constructor's legacy default remains 0.05; the checked-in config is 0.04.
- Policy ID recorded for this run: `993e26589020a5a0`. Stop cooldown is one forecast horizon, currently one hour. The initial verification recorded fresh cooldowns ending around 05:30 PT; do not assume they are still pending when reading this later.
- `configs/hyperliquid-models.json`: retrain every 900 seconds, poll 5 seconds, horizon four 15-minute bars; minimum training rows 1000, calibration 192, assessment 288, model threads 2, training window uncapped.
- Qualified means both log loss and Brier score pass the implemented comparisons against historical-frequency and 50/50 baselines on assessment data. It is model eligibility, not a direction/order instruction. Verify exact code and provenance; do not relax qualification to generate more labels.
- Recent runtime/UI changes save and show the decision's actual thresholds, role, target delta, minimum size, capacity/cash limits, stop cooldown and execution rejection reason. Historical rows must not be explained using today's config. These diagnostic changes intentionally preserved execution logic except for the explicit entry-band change from 0.05 to 0.04.
- Earlier Tkinter pack/grid resize errors were fixed. Closing/reopening the UI should not stop independently launched workers.
- Optional `max_train_rows` support exists but is disabled. A bounded offline model-window comparison did not justify promoting an alternative recipe. Independent research is at `C:/DATASTORE/hyperliquid/_model_research/20260926T103000Z-window-comparison`; it is not the deleted Paper sample.

Main code:

- `C:/dev/ducketz/ml/hyperliquid_paper_seed.py`
- `C:/dev/ducketz/ml/hyperliquid_paper_ledger.py`
- `C:/dev/ducketz/ml/hyperliquid_paper_policy.py`
- `C:/dev/ducketz/ml/hyperliquid_paper_runtime.py`
- `C:/dev/ducketz/app/services/hyperliquid_paper_view.py`
- `C:/dev/ducketz/app/services/hyperliquid_powder_view.py`
- `C:/dev/ducketz/app/ui/hyper_workspace.py`
- Corresponding `tests/test_hyperliquid_paper_*.py`, `tests/test_hyperliquid_powder_view.py`, and `tests/test_hyper_workspace.py`.

Prior implementation verification reported 412 relevant passing tests. Those tests and the seed audit did not resolve this newly reported restart problem. Add targeted regression coverage rather than relying on the old total.

## Operations and preservation constraints

At the last prior health check, **11:36:32 UTC**, Paper PID 72648 was healthy, with a committed observation at 11:36:21 UTC and 17 fresh sources. Models PID 56520 and data coordinator PID 57004 were also the recorded workers. These are not a current health report. Powder was inactive; no real trades or transfers were performed or requested by this handoff.

Hyperliquid Operations Watch is ACTIVE every 30 minutes, GPT-6 Luna / xhigh. Automation ID `hyperliquid-operations-watch`; local project ID `c899e194-7020-4972-99bb-bc40bea2ae8c`. Contract v5 is in `C:/dev/ducketz/docs/hyperliquid-system-analysis/OPERATIONS_WATCH.md`. Memory is `C:/Users/7980X/.codex/automations/hyperliquid-operations-watch/memory.md`. The watch conditionally recovers unexpected Paper/data/model exits, never reseeds or activates Powder. Coordinate intentional maintenance with its documented process. Update automations through the app tool, not by editing automation TOML.

For read-only health, use `HyperliquidWorkspaceViewService` from `app.services.hyperliquid_powder_view`. For ledger inspection, open SQLite read-only; avoid constructing a mutating ledger just to inspect data. Public account reads are sufficient for reconciliation. Do not expose credentials or invoke exchange write methods.

**Permanently excluded predecessor:** experiment `20260926T103559Z-qualified-refresh`, seed `2026-09-26T10:35:59.867633343+00:00`. The user explicitly required deletion, not archival or analysis. The user completed the cleanup command and known artifacts were verified absent. Do not restore, reconstruct for evaluation, or aggregate that sample. Registry: `C:/DATASTORE/hyperliquid/_operations/excluded-paper-runs.json`. Cleanup proof: `C:/DATASTORE/hyperliquid/_operations/excluded-paper-cleanup-verification.json`. No cleanup remains pending. This exclusion identifies a different run from the currently disputed 04:30 PT restart.

Preserve the independent model research and two still-earlier archives (`20260926T073643Z-research-and-qualified` and `20260926T102933Z-qualified-15m-v1`). Preserve current-run evidence while investigating; do not assume this latest handoff request authorizes deleting it. Keep Powder inactive and avoid unrelated trading/training changes.

Start by stating what you can establish from the current ledger and source about the opening and immediate flattening, what remains uncertain, and the concrete correction you will make. Do not simply reassure me that the mirror was verified.
