# Maintaining the Hyperliquid system reference

Last verified: **2026-09-25**. [Index](README.md) · [Changelog](CHANGELOG.md)

Update the relevant pages in the same change as behavior, configuration or
operational ownership changes. This is a living reference, not an automatically
refreshed process monitor. A date change alone is not a review.

## Change-impact matrix

| When changing… | Recheck implementation / evidence | Update these pages |
| --- | --- | --- |
| Symbols, intervals, data root | Market config, coordinator/model reload rules, Paper membership handling, retained/held assets, UI's fixed discovery/filter scope | README, LOOP_INVENTORY, LOOP_MAP, loops/market-data, loops/models, loops/paper, UI_AND_DATA_CONTRACTS |
| Feature definitions or labels | Feature schema version/catalog, causal/gap behavior, label maturity, compatible model inputs | loops/market-data, loops/models, SYSTEM_FUNCTIONALITY and component data guide |
| Ensemble, calibration or qualification | Chronological splits, maturity exclusions, model artifact identity, prior/0.5 comparisons, active-vs-candidate selection | loops/models, LOOP_INVENTORY, SYSTEM_FUNCTIONALITY and component model guide |
| Forecast horizon or cadence | Available data label columns, source-candle schedule, Paper's first-horizon selection, eligibility limits, UI horizon labels | LOOP_MAP, LOOP_INVENTORY, loops/models, loops/paper, MONITORING, UI_AND_DATA_CONTRACTS |
| Paper sizing or risk settings | Loaded config, persisted policy identity, account constraints, transfers, stop/cooldown/deduplication behavior | PAPER_ACCOUNTING_AND_RISK, loops/paper, MONITORING and component Paper guide |
| Fees, slippage, books or funding | Execution evidence, rounding/depth sharing, requested/filled quantities, settlements/proxy limitations | PAPER_ACCOUNTING_AND_RISK, loops/paper, UI_AND_DATA_CONTRACTS |
| Ledger schema or accounting | Opening seed, transfer-adjusted P/L, transaction/idempotency, restart compatibility, view queries, exports | PAPER_ACCOUNTING_AND_RISK, loops/paper, UI_AND_DATA_CONTRACTS, MONITORING |
| Runtime controls or scheduling | Ownership locks, status side effects, stop/restart behavior, actual external scheduler evidence | LOOP_INVENTORY, MONITORING, relevant loop page, README |
| Timing instrumentation or reports | Measured boundaries, work vs waits, missing durations, sample/cohort identity | MONITORING, relevant loop page and component timing guide |
| UI fields, filters or refresh | Source authority, partial states, read bounds, Tk-thread updates, destruction, visual fixtures | UI_AND_DATA_CONTRACTS, SYSTEM_FUNCTIONALITY and dated UI implementation notes |
| Automated real-money execution | Separate adapter, reconciliation, live baseline, actual fills/transfers/costs and mode provenance | Every affected page; revise Powder's status only after the implementation and verification exist |

The detailed companion guides are
[data](../hyperliquid-data-pipeline.md), [models](../hyperliquid-models.md),
[Paper](../hyperliquid-paper.md) and [timings](../hyperliquid-timings.md).
Keep their behavioral descriptions aligned with this reference. Preserve dated
measurements and design records as historical evidence instead of silently
rewriting their original observations.

## Update checklist

1. Read the changed source/configuration and identify both the producer and
   every affected consumer. A new field in a producer is not proof a UI or
   trading consumer uses it.
2. Separate implemented behavior, configured parameters and dated observations.
   Record future proposals as planned; do not describe them as current.
3. Update the smallest relevant pages and their cross-links. Keep the index/map
   current when a component or relationship is added, removed or renamed.
4. Check units and clocks: UTC versus local labels, percentages versus fractions,
   bars versus minutes, input close versus availability, work versus polling,
   historical entry versus opening performance, and requested versus filled.
5. Record verification appropriate to the behavior. Link tests and dated visual
   evidence; distinguish commands merely documented from checks actually run.
6. Update each reviewed page's verification date and add a concise entry to
   [CHANGELOG.md](CHANGELOG.md) explaining the contract change and evidence.
7. Check relative Markdown links and source paths. Link source files/symbols
   instead of depending on line numbers that drift after edits.

## Evidence conventions

Use this small record for a substantive change or investigation:

```text
Date / timezone:
Change or question:
Code/configuration revision:
Affected source and consumers:
Implemented / configured / observed / planned:
Behavior before → after:
Evidence timestamps and artifact identities:
Checks actually run and results:
Operational or compatibility impact:
Remaining uncertainty:
Documentation pages updated:
```

Put lengthy timestamped investigations under a dated `audits/` subdirectory
when there is an actual investigation to preserve. The normal architecture
pages should link such a record rather than accumulating changing PIDs,
balances, probabilities and log excerpts. Do not copy credentials, private
keys or environment secrets into documentation.

## Verification map

These are existing test families to select when the corresponding code changes.
Document-only edits normally need source review and link checking, not a worker
restart or a repeated model fit.

| Area | Test files under `tests/` |
| --- | --- |
| Candles/features/publication | `test_hyperliquid_market_data.py`, `test_hyperliquid_features.py`, `test_hyperliquid_data_pipeline.py`, `test_hyperliquid_catchup.py` |
| Data process/configuration | `test_hyperliquid_data_loop.py`, `test_hyperliquid_coordinator.py`, `test_hyperliquid_coordinator_config.py` |
| Models | `test_hyperliquid_model_config.py`, `test_hyperliquid_models.py`, `test_hyperliquid_model_artifacts.py`, `test_hyperliquid_model_runtime.py` |
| Paper policy/execution/accounting | `test_hyperliquid_paper_policy.py`, `test_hyperliquid_paper_market.py`, `test_hyperliquid_paper_seed.py`, `test_hyperliquid_paper_ledger.py`, `test_hyperliquid_paper_runtime.py` |
| Powder execution/recovery | `test_hyperliquid_powder_runtime.py`, `test_hyperliquid_powder_exchange.py`, `test_hyperliquid_powder_ledger.py`, `test_hyperliquid_forecast_reader.py`, `test_hyperliquid_powder_view.py` (fake exchanges and temporary ledgers only) |
| Durations | `test_hyperliquid_timings.py` |
| H.Y.P.E.R. and manual-workspace integration | `test_hyperliquid_paper_view.py`, `test_hyper_workspace.py`, `test_ducket_bucket_app.py`, `test_hyperliquid_duckets_ui.py`, `test_hyperliquid_workspace_services.py` |

Run selected tests with the repository's Python environment, for example:

```powershell
& .\.venv\Scripts\python.exe -m pytest tests/test_hyperliquid_paper_view.py tests/test_hyper_workspace.py -q
```

For UI changes, inspect both Paper and Powder at normal and compact widths,
including the scrolled panels. The
[fixture](../../tests/visual_hyper_workspace_fixture.py) supports reproducible
offline snapshots and a separately labeled one-time local-data preview.

## Do not use a documentation update as a runtime operation

Reading or editing these pages does not require starting, stopping, reseeding,
retraining, cleaning run folders or changing a schedule. Lifecycle and export
commands in [MONITORING.md](MONITORING.md) have different side effects and are
labeled accordingly. Preserve existing data/history and unrelated work in the
checkout. An observation that differs from prose is a reason to investigate
and correct the reference, not to force the system back to an old example.
