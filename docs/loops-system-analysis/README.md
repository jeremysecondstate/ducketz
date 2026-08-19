# Ducketz Loops system analysis

This directory documents the current production Loops implementation. Code, immutable receipts, and the datastore health output are authoritative; these pages are explanatory and are not operational proof that a provider is connected or that a partition still exists.

## Current baseline

- **Confirmed:** the startup document declares seven independent runtime owners and says they coordinate through verified atomic pointers. `docs/datafetch-ml/current_start_command:3`, `docs/datafetch-ml/current_start_command:5`
- **Confirmed by code census:** exactly seven recurring production supervisors exist: CME/L2, Loop A, Daily ALFRED, Active Pricing, Options Capture, Directional Loop B, and Strategy.
- **Confirmed:** `ml.option_pricing_loop_native_worker` is a one-shot, non-blocking child owned by Active Pricing, not an eighth independent loop. `ml/option_pricing_loop_native_worker.py:38`, `ml/option_pricing_loop_native_worker.py:135`, `ml/option_pricing_runtime.py:440`
- **Confirmed deployment:** the checked-in launcher derives its seven commands from `ml.system_guardian.GUARDIAN_LAUNCHES`, verifies `ml.system_monitor.RUNTIMES`, existing process pairs, and worker-owned locks before acting, starts only a completely missing owner, and uses resolved paths, explicit working directory, redirected primary logs, unbuffered Python, and hidden windows. Options retains `--skip-historical-catchup`. `docs/datafetch-ml/start_all_loops.ps1:18`, `ml/system_guardian.py:81`
- **Confirmed:** `datafetching.options_history` is the one-time per-symbol OPRA bootstrap. `datafetching.databento_cold_start` is the optional one-time all-dataset bootstrap and hands verified OPRA scopes to Options through v5 symbol/schema history cursors. Both normal paths use the included Standard-plan windows, validate free capacity with a 5 GiB reserve and 2× expansion allowance, and fail closed on scope or evidence mismatch. Options Capture owns recurring catch-up for completed cursors; none of these maintenance paths creates an eighth supervisor. `datafetching/options_history.py`, `datafetching/databento_cold_start.py`, `datafetching/options_runtime.py`
- **Confirmed archive separation:** Loop A uses current `EQUS.MINI` operational bars under `stocks`; the different-dataset `XNAS.ITCH` equity archive remains cold provenance and is not timestamp-merged into that view. The bridge remains available only for matching dataset identities. CME still derives a missing initial runtime boundary from verified archive scope, fingerprints archive inventories, combines archive and persisted live rows, and materializes unseen context windows. `datafetching/databento_archive.py:213`, `datafetching/databento_archive.py:539`, `datafetching/databento_fetch.py:541`, `datafetching/equity_dataset_migration.py`, `datafetching/cme_cross_asset_context.py:250`
- **Confirmed monitoring:** hourly mode covers ownership, locks, logs, publications, lineage, UIs, and storage; daily adds every directional route plus Strategy/Pricing outcome evaluation; weekly adds a comparable immutable-evidence roll-up after the final eligible XNYS session of the week. Insufficient weekly observations are reported explicitly rather than converted into a trend. `ml/system_monitor.py:164`, `ml/system_monitor.py:1375`, `ml/system_monitor.py:1872`
- **Operational boundary:** historical files are mutable production state. Verify the current state with `python -m ml.option_pricing_opra --datastore-target pc --health-only` and the receipts beneath `C:\DATASTORE\market-data\databento\opra\OPRA.PILLAR`; do not infer population from these docs or from `provider-mode=opra-canonical`.

**Observed 2026-08-19 11:15 UTC:** all seven launcher/worker pairs and matching
worker locks passed. Active legacy `C:\DATASTORE\runtime-logs` streams became
monitor-visible without restarting healthy owners; Loop B advanced to a fresh
publication on its own. Strategy-to-Loop-B lineage was still warning at that
observation and remained publication-health evidence for the normal +10 cycle.
No PID is architectural and none is recorded here.

## Evidence labels

Every conclusion uses one of these labels:

- **Confirmed:** directly established by executable implementation or an explicit contract; tests may provide corroboration.
- **Inferred:** the strongest explanation supported by multiple code paths, but not explicitly declared as a contract.
- **Documented only:** prose or startup intent not independently implemented as coordination.
- **Conflict:** implementation, commands, tests, or documentation disagree.
- **Unknown:** the repository does not establish the answer.

Repository citations use `relative/path:line`. A citation names the line where the relevant definition, condition, or contract begins; adjacent implementation lines complete the cited construct.

## Executive result

**Confirmed:** the system has seven independent production loops:

| # in startup order | Canonical loop | Runtime entry point | Roll-up contribution |
|---:|---|---|---|
| 1 | CME/L2 runtime | `datafetching.cme_runtime` | Both |
| 2 | Loop A | `datafetching.orchestrate` | Both |
| 3 | Daily ALFRED runtime | `datafetching.fred_alfred_runtime` | Both |
| 4 | Active Pricing (logical Loop 3) | `ml.option_pricing_runtime` | Both |
| 5 | Options Capture (logical Loop 4) | `datafetching.options_runtime` | Both |
| 6 | Directional Loop B | `ml.prediction_runtime` | Both |
| 7 | Strategy runtime | `ml.strategy_runtime` | Options |

“Both” means the loop has an evidenced causal path to at least one directional-horizon output and to at least one options-family output (option pricing or options strategy). “Direct” is reserved for the loop that publishes that prediction family’s authoritative artifact; upstream causal inputs are “Indirect.” This prevents temporal proximity alone from counting as contribution. The detailed basis is in [Prediction contribution matrix](PREDICTION_CONTRIBUTION_MATRIX.md).

At a high level, Loop A freezes exact equity-bar readiness and later a complete provider/feature cycle; CME/L2 and Daily ALFRED independently publish cross-asset and vintage-macro evidence. CME and Loop A verify and bridge available archive baselines, then continue under bounded overlapping owned-runtime contracts; Daily ALFRED requires its documented one-time causal backfill sequence. Options Capture owns prospective provider-neutral option evidence through one scoped, bounded OPRA `cbbo-1s` live adapter and retains Schwab as labeled per-target fallback/broker evidence. A separate one-time per-parent command, or the `--confirm-download` all-dataset cold-start coordinator, bootstraps included Standard history. Both publish only verified v5 history-cursor handoffs; Options Capture subsequently performs one daily, schema-specific overlap catch-up for valid cursors. Active Pricing and eligible offline Strategy workflows read verified OPRA partitions; recurring live Strategy selection requires prospective receipts and forbids offline replay. `datafetching/options_history.py`, `datafetching/databento_cold_start.py`, `datafetching/databento_archive.py:213`, `datafetching/databento_archive.py:539`, `datafetching/options_runtime.py`, `ml/strategy_selection/runtime.py:167`

Historical OPRA replay/cache is eligible for receipt-verified offline Pricing
evaluation and Strategy outcome/model construction. Prospective receipts remain
the preferred live evidence, and recurring Strategy entry plus live Pricing
attachment explicitly forbid offline replay. Scenario Coverage is a heuristic
scenario-grid pass fraction, not a probability; calibrated Strategy fields stay
null until the fitted causal model and full eligible Pricing coverage exist.
`ml/option_pricing_opra_replay.py:224`,
`ml/strategy_selection/runtime.py:167`,
`ml/strategy_selection/contracts.py:33`

## Deliverables

- [Loop inventory and classification](LOOP_INVENTORY.md)
- [System functionality](SYSTEM_FUNCTIONALITY.md)
- [Loop relationships](LOOP_RELATIONSHIPS.md)
- [Visual loop map](LOOP_MAP.md)
- [Prediction contribution matrix](PREDICTION_CONTRIBUTION_MATRIX.md)
- [Monitoring and guarded recovery](MONITORING.md)
- Per-loop reports:
  - [CME/L2 runtime](loops/cme-l2-runtime.md)
  - [Loop A](loops/loop-a.md)
  - [Daily ALFRED runtime](loops/daily-alfred-runtime.md)
  - [Active Pricing / logical Loop 3](loops/active-pricing-loop-3.md)
  - [Options Capture / logical Loop 4](loops/options-capture-loop-4.md)
  - [Directional Loop B](loops/directional-loop-b.md)
  - [Strategy runtime](loops/strategy-runtime.md)
