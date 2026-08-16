# Ducketz Loops system analysis

This directory documents the current production Loops implementation. Code, immutable receipts, and the datastore health output are authoritative; these pages are explanatory and are not operational proof that a provider is connected or that a partition still exists.

## Current baseline

- **Confirmed:** the startup document declares seven independent runtime owners and says they coordinate through verified atomic pointers. `docs/datafetch-ml/current_start_command:3`, `docs/datafetch-ml/current_start_command:5`
- **Confirmed by code census:** exactly seven recurring production supervisors exist: CME/L2, Loop A, Daily ALFRED, Active Pricing, Options Capture, Directional Loop B, and Strategy.
- **Confirmed:** `ml.option_pricing_loop_native_worker` is a one-shot, non-blocking child owned by Active Pricing, not an eighth independent loop. `ml/option_pricing_loop_native_worker.py:38`, `ml/option_pricing_loop_native_worker.py:135`, `ml/option_pricing_runtime.py:440`
- **Confirmed:** `datafetching.options_history` is the one-time per-symbol OPRA bootstrap. `datafetching.databento_cold_start` is the optional one-time all-dataset bootstrap and hands verified OPRA scopes to Options through v5 symbol/schema history cursors. Options Capture owns recurring catch-up for completed cursors; none of these maintenance paths creates an eighth supervisor. `datafetching/options_history.py`, `datafetching/databento_cold_start.py`, `datafetching/options_runtime.py`
- **Operational boundary:** historical files are mutable production state. Verify the current state with `python -m ml.option_pricing_opra --datastore-target pc --health-only` and the receipts beneath `C:\DATASTORE\market-data\databento-opra\OPRA.PILLAR`; do not infer population from these docs or from `provider-mode=opra-canonical`.

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

At a high level, Loop A freezes exact equity-bar readiness and later a complete provider/feature cycle; CME/L2 and Daily ALFRED independently publish cross-asset and vintage-macro evidence. Options Capture owns prospective provider-neutral option evidence through one scoped, bounded OPRA `cbbo-1s` live adapter and retains Schwab as labeled per-target fallback/broker evidence. A separate one-time per-parent command, or the explicitly confirmed all-dataset cold-start coordinator, bootstraps Standard history. Both publish only verified v5 history-cursor handoffs; Options Capture subsequently performs one daily, schema-specific overlap catch-up for valid cursors. Active Pricing and Strategy read verified OPRA partitions first and use Schwab only where their contracts allow fallback. `datafetching/options_history.py`, `datafetching/databento_cold_start.py`, `datafetching/options_runtime.py`, `ml/option_pricing/opra_materialization.py`, `ml/strategy_selection/chain.py`

## Deliverables

- [Loop inventory and classification](LOOP_INVENTORY.md)
- [System functionality](SYSTEM_FUNCTIONALITY.md)
- [Loop relationships](LOOP_RELATIONSHIPS.md)
- [Visual loop map](LOOP_MAP.md)
- [Prediction contribution matrix](PREDICTION_CONTRIBUTION_MATRIX.md)
- Per-loop reports:
  - [CME/L2 runtime](loops/cme-l2-runtime.md)
  - [Loop A](loops/loop-a.md)
  - [Daily ALFRED runtime](loops/daily-alfred-runtime.md)
  - [Active Pricing / logical Loop 3](loops/active-pricing-loop-3.md)
  - [Options Capture / logical Loop 4](loops/options-capture-loop-4.md)
  - [Directional Loop B](loops/directional-loop-b.md)
  - [Strategy runtime](loops/strategy-runtime.md)
