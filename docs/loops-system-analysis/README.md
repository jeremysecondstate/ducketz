# Ducketz Loops system analysis

This directory is a static, evidence-backed architectural audit of the production Loops system. The audit did not start a runtime, contact a provider, execute OPRA acquisition, read `.env`, or inspect or modify `C:\DATASTORE`. Only the Markdown files in this directory were created.

## Audit baseline

- **Confirmed (Git baseline recorded before audit writes):** branch `main`; commit `1c88c423cb39bb092e30825a024d5db641170526`; working tree clean at audit start (`## main...origin/main` with no changed paths).
- **Confirmed:** the startup document declares seven independent runtime owners and says they coordinate through verified atomic pointers. `docs/datafetch-ml/current_start_command:3`, `docs/datafetch-ml/current_start_command:5`
- **Confirmed by static census:** exactly seven recurring production supervisors contain the top-level recurring `while True` cycles: CME/L2, Loop A, Daily ALFRED, Active Pricing, Options Capture, Directional Loop B, and Strategy. `datafetching/cme_runtime.py:382`, `datafetching/orchestrate.py:152`, `datafetching/fred_alfred_runtime.py:156`, `ml/option_pricing_runtime.py:1564`, `datafetching/options_runtime.py:648`, `ml/prediction_runtime.py:192`, `ml/strategy_runtime.py:361`
- **Confirmed:** `ml.option_pricing_loop_native_worker` is a one-shot, non-blocking child owned by Active Pricing, not an eighth independent loop. `ml/option_pricing_loop_native_worker.py:38`, `ml/option_pricing_loop_native_worker.py:135`, `ml/option_pricing_runtime.py:440`
- **Method:** static implementation, contract, test, startup-command, and existing-flowchart analysis. Tests were inspected but not executed because implementation and test contracts resolved the architectural questions without creating caches or touching runtime data.

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

At a high level, Loop A freezes exact equity-bar readiness and later a complete provider/feature cycle; CME/L2 and Daily ALFRED independently publish cross-asset and vintage-macro evidence. Options Capture owns prospective provider-neutral option evidence, with an implemented injected OPRA `cbbo-1s` lane and explicit Schwab fallback; the separate historical OPRA importer supplies verified `cbbo-1m` evidence to Pricing/model training but is maintenance, not an eighth loop. Active Pricing uses exact Loop A readiness, earlier option chains, causal rates, and a prior local residual model to publish target-time contract values. Its model follows the repository's implemented `BLACK-SCHOLES-OP` pattern—Black–Scholes mean plus a learned residual and predictive uncertainty—using a bounded Nyström-RBF/Bayesian-ridge approximation rather than the thesis's exact GP. Directional Loop B consumes the complete Loop A generation plus verified feature authorities, including gated Pricing surfaces, and publishes calibrated horizon probabilities. Strategy consumes Loop B, exact Schwab execution-chain receipts, stock BBO evidence, and active BSGP-or-Black–Scholes leg pricing, then publishes profitable-outcome probabilities and ranks. `options/providers.py:25`, `ml/option_pricing/opra.py:31`, `docs/edu/BLACK-SCHOLES-OP.md:441`, `ml/option_pricing/model.py:68`, `ml/option_pricing/strategy_shadow.py:298`

## Deliverables

- [Loop inventory and classification](LOOP_INVENTORY.md)
- [System functionality](SYSTEM_FUNCTIONALITY.md)
- [Loop relationships](LOOP_RELATIONSHIPS.md)
- [Visual loop map](LOOP_MAP.md)
- [Prediction contribution matrix](PREDICTION_CONTRIBUTION_MATRIX.md)
- [Scoped recommendations](RECOMMENDATIONS.md)
- Per-loop reports:
  - [CME/L2 runtime](loops/cme-l2-runtime.md)
  - [Loop A](loops/loop-a.md)
  - [Daily ALFRED runtime](loops/daily-alfred-runtime.md)
  - [Active Pricing / logical Loop 3](loops/active-pricing-loop-3.md)
  - [Options Capture / logical Loop 4](loops/options-capture-loop-4.md)
  - [Directional Loop B](loops/directional-loop-b.md)
  - [Strategy runtime](loops/strategy-runtime.md)

## Current-status boundaries and resolved legacy references

- **Confirmed current authority:** the startup command and executable supervisors agree on seven owners. The older SVG's six-owner picture is retained only as an obsolete reference and is not a current implementation conflict. `docs/datafetch-ml/current_start_command:3`, `datafetching/fred_alfred_runtime.py:156`, `ml/strategy_runtime.py:361`
- **Confirmed aliasing, not conflict:** “logical Loop 3” and “logical Loop 4” are stable functional aliases; startup owner numbers 4 and 5 include Daily ALFRED as owner 3. `docs/datafetch-ml/current_start_command:61`, `docs/datafetch-ml/current_start_command:72`, `docs/datafetch-ml/current_start_command:95`
- **Confirmed implemented OPRA paths:** the repository contains an explicitly authorized, resumable historical `OPRA.PILLAR` importer and an injected prospective `cbbo-1s` adapter contract with immutable provider-neutral publication and labeled Schwab fallback. Tests establish idempotent OPRA capture and fail-closed paid-import controls. `ml/option_pricing_opra.py:35`, `ml/option_pricing/opra.py:1049`, `datafetching/options_runtime.py:362`, `tests/test_option_publication.py:403`, `tests/test_option_pricing_opra.py:313`
- **Confirmed implemented Black–Scholes/residual setup:** the thesis's six-input `f(x)=BS(x)+delta(x)` structure is represented by the six semantic inputs and a 128-component Nyström RBF residual model with Bayesian-ridge posterior, calibrated uncertainty, no-arbitrage projection, and Black–Scholes fallback. Ready residual rows are actively consumable by Strategy; the fast option-pricing target keeps its separately published Black–Scholes baseline authority. `docs/edu/BLACK-SCHOLES-OP.md:327`, `docs/edu/BLACK-SCHOLES-OP.md:441`, `ml/option_pricing/policies.py:21`, `ml/option_pricing/prediction.py:110`, `ml/option_pricing/strategy_shadow.py:298`
- **Confirmed rollout boundary:** the numbered Options CLI does not instantiate a concrete prospective OPRA transport; the repository explicitly identifies a live adapter and credentials as a rollout dependency, while the checked-in command captures the Schwab broker lane. This limits claims about current live provider mix but not the confirmed OPRA ingestion implementation. `datafetching/options_runtime.py:663`, `options/README.md:29`, `docs/datafetch-ml/current_start_command:99`
- **Unknown operational state:** because the audit did not inspect secrets or `C:\DATASTORE`, it does not claim that runtimes are running, a live OPRA adapter is configured, historical acquisition has executed, gates currently pass, artifacts are populated, or realized accuracy matches either repository assessment results or the thesis.

## Final verification record

- **Confirmed:** seven inventory entries are classified as independent production loops and exactly seven files exist under `loops/`, one for each.
- **Confirmed:** every loop report has Inputs, Outputs, Direct loop relationships, and Prediction contribution sections.
- **Confirmed:** the Mermaid primary map contains each of the seven independent loops exactly once; its edge-evidence table cites producer and consumer implementation.
- **Confirmed:** both Mermaid blocks have balanced fences and use standard `flowchart LR`/`sequenceDiagram` grammar; every edge is labeled, and the flowchart’s node, subgraph, link, class, and style declarations passed the static syntax check.
- **Confirmed:** recommendations are limited to prediction accuracy or efficiency that preserves accuracy and causal evidence.
- **Confirmed:** all 1,703 repository citations resolve to existing files and in-range lines; all 14 relative Markdown links resolve.
- **Confirmed:** the final working-tree delta consists only of the 14 requested Markdown files under this directory; nothing was staged.
