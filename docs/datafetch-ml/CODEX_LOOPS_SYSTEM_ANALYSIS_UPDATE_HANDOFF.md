# Codex handoff: update the Loops system analysis docs

Paste the prompt below into a new Codex task rooted at `C:\dev\ducketz`.

## Handoff prompt

You are responsible for reconciling every document under
`docs/loops-system-analysis/` with the current Loops implementation and the
latest verified production evidence. This is a documentation task, but it is
not a superficial timestamp refresh: re-audit the executable contracts,
provider boundaries, publications, monitoring semantics, relationships, and
every repository citation before changing the prose.

### Expected state at handoff

Treat this section as a set of leads to verify locally, not as proof to copy.

- The production repository is `C:\dev\ducketz`; datastore target `pc` resolves
  to `C:\DATASTORE`.
- The production watchlist is exactly `AAPL AMZN GOOG MU NVDA SNDK`.
- There are exactly seven recurring production owners: CME/L2, Daily ALFRED,
  Loop A, Active Pricing, Directional Loop B, Options Capture, and Strategy.
- At the last proof, every owner had one canonical launcher/worker pair and a
  matching worker-owned singleton lock. The pairs were:
  - CME `49712/50132`
  - ALFRED `26512/53592`
  - Loop A `35852/5516`
  - Active Pricing `22904/56844`
  - Directional Loop B `23480/8008`
  - Options Capture `42072/52628`
  - Strategy `51280/10964`
  These PIDs are transient evidence. Recheck them, and do not turn them into
  architectural documentation.
- The final post-activation read-only monitor was `HEALTHY` at
  `2026-08-19T22:45:36.737912Z`: 19 `PASS`, 1 `INFO`, 0 `WARN`, 0 `FAIL`, no
  occurrence of `STALE`, `read_only=true`, and `orders_placed=0`. The preserved
  report is:
  `C:\DATASTORE\logs\ducketz\restart-proof\20260819T200349.8245990Z\20260819T224536.737912+0000-monitor-post-activation-stale-free-healthy.json`.
- The one `INFO` was market-aware and intentional: the regular XNYS option
  evidence window was closed, so Active Pricing had no eligible target and
  correctly refused to backdate or fabricate one.
- The latest completed regular market target was
  `2026-08-19T20:00:00Z`. The checksum-valid six-symbol readiness receipt is
  `C:\DATASTORE\loop-a\bar-readiness\1787169600000000000\receipt.json`.
- Loop A generation `20260819T223020.010102Z-pid5516` finished at
  `2026-08-19T22:35:52.180453Z` with zero failures and all six symbols. A later
  cycle had already begun by the final monitor; distinguish the active cycle
  from `last_complete_generation` when documenting it.
- Canonical operational equity OHLCV is Databento `EQUS.MINI`. The current raw
  1-minute files for all six symbols contain only
  `provider_dataset=EQUS.MINI` and `source_schema=ohlcv-1m`. Schwab may provide
  quotes, option chains, broker evidence, and explicitly labeled fallback
  evidence, but it is not canonical operational OHLCV.
- The separate cold equity archive remains `XNAS.ITCH`. Its different dataset
  identity is deliberately not merged into the operational `EQUS.MINI` view.
- The fresh Directional Loop B authority is
  `C:\DATASTORE\ml\runs\20260819T223552.337574Z`. Its manifest and publication
  receipt verified. It contains 54 intelligence rows, all
  `OPERATIONALLY_CURRENT`, with no stale cell. All six symbols have the primary
  `1h`, `4h`, `1d`, and `1w` routes. Because the decision was on Wednesday, the
  valid frozen remaining-week bundle is `1w`, `1w-d1` (Thursday), and `1w-d2`
  (Friday); `1w-d3` through `1w-d5` are calendar-inapplicable route slots, not
  missing forecasts.
- The fresh Strategy authority is
  `C:\DATASTORE\ml\strategy-runs\20260819T224000.073641Z`. It is checksum-bound
  to that exact Loop B run and published 4,800 candidates plus 1,440 audit
  rows. Cross-loop lineage and both UI publication contracts passed.
- The six option snapshots for the 20:00 target are checksum-valid, explicitly
  labeled Schwab fallbacks. The Databento OPRA live adapter reported
  `OPRA_TARGET_WATERMARK_UNAVAILABLE`; the fallback did not silently become
  canonical OPRA or equity OHLCV.
- The current CME L2 publication is
  `C:\DATASTORE\pools\cme\snapshots\l2\databento\5m\1787145000000000000`.
  It is a strict, checksum-valid `GLBX.MDP3` current operational snapshot with
  complete configured BBO/MBP coverage. The current configured gold contract
  was `GCZ6`; treat a dated contract symbol as an observation, not an evergreen
  architecture rule.
- The current ALFRED receipt is
  `C:\DATASTORE\ml\fred-alfred-runtime\20260819T070003.938741Z\receipt.json`.
- The existing Codex heartbeat `loops-hourly-operations` was updated in place
  and verified `ACTIVE`; its name, full prompt, hourly minute-42 cadence,
  heartbeat kind, and target task were preserved. No duplicate was created.

The restart work also changed implementation details that the docs must audit:

- `app/services/databento_retry.py` recognizes a prematurely ended Databento
  response as retryable.
- `options/databento_live.py` cooperatively yields during callback replay so
  the Databento client callback thread is not starved.
- `datafetching/cme_runtime.py` and `datafetching/cme_history.py` separate the
  current operational L2 lane from bounded historical recovery, use strict
  expected-symbol freshness, exact short BBO/MBP windows, configured-symbol
  authority, and a distinct availability cutoff.
- `ml/runtime_pipeline.py` classifies omitted dynamic remaining-week component
  slots as `NOT_APPLICABLE_TO_REMAINING_WEEK` and
  `OPERATIONALLY_CURRENT` only when a coherent created-LIVE weekly bundle for
  that symbol proves the calendar-correct prefix. A genuinely missing,
  malformed, or ambiguous weekly bundle remains fail-closed and stale.

### Safety and scope

1. Run `git status` first. Preserve every existing change and never discard,
   overwrite, reformat, or fold unrelated work into the documentation patch.
2. This task authorizes edits only beneath `docs/loops-system-analysis/`.
   Do not edit code, tests, `.env`, datastore artifacts, automation settings,
   or other documentation. If a code or configuration defect is discovered,
   report it as a documentation blocker or conflict instead of fixing it.
3. Do not start, stop, restart, signal, or kill any process. Do not remove or
   move locks. Do not run the guardian with repair enabled.
4. Do not download provider history, run OPRA bootstrap/cold-start/backfill,
   rewrite a pointer, promote a model, clean the datastore, or place, preview,
   or submit an order.
5. Never print credentials. Credential presence is not necessary to update
   these docs; use checked-in configuration and already-published evidence.
6. Read-only process inspection and this monitor command are allowed:

   ```powershell
   .\.venv\Scripts\python.exe -m ml.system_monitor --datastore-target pc --mode scheduled --compact
   ```

   Run it only when current operational evidence materially helps the docs.
   Parse the JSON even on a nonzero exit, preserve any divergence, and never
   use a guardian repair call merely to make the observation look healthy.
7. Code, tests, immutable receipts, and monitor output outrank existing prose.
   Keep the directory's `Confirmed`, `Inferred`, `Documented only`, `Conflict`,
   and `Unknown` evidence vocabulary. Do not turn inference or a one-time
   observation into a permanent contract.
8. Keep timestamps, PIDs, current futures contracts, run paths, row counts, and
   provider fallbacks explicitly labeled as observations. Architectural
   sections should describe the durable selection and validation rules.
9. Do not hide an unresolved contradiction by deleting it. Label it `Conflict`
   or `Unknown`, cite the competing evidence, and explain what would resolve
   it.

### Read these authorities before editing

Read every Markdown file under `docs/loops-system-analysis/` completely. Then
read the current implementation and tests relevant to the changed facts:

- `ml/system_guardian.py`
- `ml/system_monitor.py`
- `docs/datafetch-ml/start_all_loops.ps1`
- `docs/datafetch-ml/current_start_command`
- `datafetching/watchlist.txt`
- `datafetching/orchestrate.py`
- `datafetching/loop_a_cycle.py`
- `datafetching/bar_readiness.py`
- `app/services/databento_market_data.py`
- `app/services/databento_retry.py`
- `datafetching/databento_fetch.py`
- `datafetching/databento_archive.py`
- `datafetching/equity_dataset_migration.py`
- `datafetching/cme_runtime.py`
- `datafetching/cme_history.py`
- `datafetching/cme_cross_asset_context.py`
- `options/databento_live.py`
- `datafetching/options_runtime.py`
- `options/snapshot.py`
- `options/publication.py`
- `ml/prediction_runtime.py`
- `ml/runtime_pipeline.py`
- `ml/horizons.py`
- `ml/rolling_materialization.py`
- `ml/strategy_runtime.py`
- `ml/strategy_publication.py`
- `app/ui/rolling_forecast_data.py`
- `app/ui/options_strategy_data.py`
- `tests/test_databento_retry.py`
- `tests/test_databento_opra_live.py`
- `tests/test_cme_runtime.py`
- `tests/test_ml_weekly_context_model_runtime.py`
- `tests/test_ml_runtime_pipeline.py`
- `tests/test_runtime_ui_integration.py`
- `tests/test_ui_rolling_forecasts.py`
- `tests/test_system_monitor.py`

Use the preserved final monitor JSON and the immutable run/receipt paths above
as production evidence. Re-verify checksums through the repository's readers
where available instead of trusting filenames. Do not assume a file remains
current merely because it exists.

### Required documentation reconciliation

1. Build a claim/citation audit before editing. Identify:
   - prose that is now false or incomplete;
   - observations that still describe the earlier 11:15 UTC lineage warning as
     current;
   - citations whose line numbers moved or whose implementation meaning
     changed;
   - duplicated claims that would diverge if only one page were updated; and
   - durable architecture versus timestamped production observations.

2. Reconfirm the inventory. Keep exactly the seven independent owners unless
   executable evidence proves otherwise. Preserve the distinction between a
   recurring owner, an owned one-shot child, bounded polling, provider
   transport, one-time maintenance, compatibility lanes, and read-only UI
   consumers.

3. Reconcile provider and authority boundaries everywhere they appear:
   - operational equity OHLCV: Databento `EQUS.MINI`;
   - cold equity archive: separate `XNAS.ITCH` provenance;
   - prospective option authority: scoped Databento `OPRA.PILLAR`, with only a
     bounded, explicit Schwab fallback for eligible unavailability;
   - Schwab quotes/chains/broker evidence never silently becoming canonical
     equity OHLCV or OPRA; and
   - CME `GLBX.MDP3` current authority remaining separate from archive baseline
     and recovery evidence.

4. Update the durable runtime descriptions where the code changed:
   - Loop A's retry behavior and exact market-aware readiness/cycle distinction;
   - CME's current strict L2 lane, availability boundary, configured-symbol
     override, recovery chunking, and checksum/freshness requirements;
   - Options' live callback scheduling, target-watermark requirement, and
     explicitly labeled fallback behavior;
   - Directional Loop B's dynamic frozen remaining-week prefix and the precise
     fail-closed rule for calendar-inapplicable versus genuinely absent routes;
   - Strategy's exact current Loop B lineage requirement; and
   - Active Pricing's market-aware no-target informational state without
     backdating or pointer fabrication.

5. Reconcile all affected pages, not just the per-loop reports. At minimum,
   evaluate whether each of these needs a change:
   - `README.md`
   - `LOOP_INVENTORY.md`
   - `SYSTEM_FUNCTIONALITY.md`
   - `LOOP_RELATIONSHIPS.md`
   - `LOOP_MAP.md`
   - `PREDICTION_CONTRIBUTION_MATRIX.md`
   - `MONITORING.md`
   - every file under `loops/`

6. Replace operational-health prose that still presents the 11:15 UTC
   Strategy-to-Loop-B warning as current with a newly verified, timestamped
   observation. The 22:45 proof established healthy current lineage and
   stale-free UI contracts. Retain older OPRA replay/model measurements only if
   they remain useful as explicitly historical evidence; do not silently
   relabel an old measurement as current without re-running its verifier.

7. Document the semantic distinction exposed by the final monitor:
   `HEALTHY` may include a benign `INFO` when the market-aware contract requires
   no Active Pricing target. It may not contain an unresolved `WARN`, `FAIL`,
   or stale condition. Process/lock health, publication freshness, provider
   authority, cross-loop lineage, and UI contract health remain separate
   conclusions.

8. Preserve the existing document organization, concise evidence labels,
   tables, Mermaid diagrams, and relative links. Update diagrams and matrices
   when a changed rule affects an edge or contribution classification. Avoid
   adding a changelog dump to every page; integrate facts into the sections
   where a future operator or engineer would look for them.

9. Recalculate every `relative/path:line` citation you touch against the final
   working tree. A valid path with a stale line number is not an acceptable
   citation. Cite the start of the relevant definition or contract and ensure
   the adjacent code actually supports the prose.

### Validation

Run documentation-focused and relevant bounded checks:

```powershell
rg -n "2026-08-19 11:15|Strategy-to-Loop-B.*warn|OPERATIONALLY_STALE|GCQ6" docs\loops-system-analysis
.\.venv\Scripts\python.exe -m pytest tests\test_databento_retry.py tests\test_databento_opra_live.py tests\test_cme_runtime.py tests\test_ml_weekly_context_model_runtime.py tests\test_ml_runtime_pipeline.py tests\test_runtime_ui_integration.py tests\test_ui_rolling_forecasts.py tests\test_system_monitor.py -q
git diff --check
git status --short
```

Interpret the `rg` results; do not blindly replace every historical occurrence.
Also perform a bounded local validation that:

- every relative Markdown link resolves;
- every cited repository path exists;
- every cited line number is within the current file and begins the claimed
  construct;
- Mermaid fences are balanced and node/edge identifiers remain coherent;
- the README deliverable list still covers every document; and
- terminology, route names, provider roles, authority paths, and evidence
  labels agree across the whole directory.

If a fresh read-only monitor is no longer healthy, do not mutate production and
do not overwrite the preserved 22:45 observation with a false success claim.
Record the new timestamped divergence accurately, distinguish transient state
from architecture, and report the exact blocker.

Finish with a concise report listing the documentation files changed, the main
contracts and observations reconciled, any conflict or unknown left in place,
the validation commands and results, and final `git status`. Do not claim the
docs are current if citations, links, cross-document terminology, or production
observations remain inconsistent.
