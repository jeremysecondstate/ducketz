# Prospective XNAS.BASIC integration scope

Read-only code audit, September 13, 2026 UTC. This is a proposal pending the user's
source-policy choice. It makes no source switch, production edit, publication,
model fit, scheduler change, or trading-control change.

## Minimal safe architecture

Add one explicit new contract in a centralized source registry. The selected
contract must bind dataset **and accepted-observation semantics**. If production
qualification excludes native zero-volume bars, encode that policy in the new
contract/configuration; do not silently change it later under the same name.
Keep both existing contracts and defaults unchanged. Acquire and retain complete
native BASIC data independently; derive a manifest-bound eligible-observation view
with explicit reasons/counts for every excluded row or date. Do not rewrite raw
volume or mark provider-created zero-volume records as local synthetic bars.

Use the existing native per-dataset archive layout, manifests, raw checksums,
request cursors, exact zero-dollar preflight, capacity checks, and single owner.
Generalize only the dataset-specific history/reader decisions. Do not expand the
ITCH Live fallback to BASIC: Historical availability must fail visibly, preserving
the current prohibition on retrying the ITCH Live license denial.

## Code surfaces requiring implementation or explicit regression coverage

| Surface | Current behavior and required treatment |
|---|---|
| [Source registry and reader](C:/dev/ducketz/ml/stock_target_prices.py:19) | Only canonical EQUS.MINI and XNAS.ITCH exist. The archive branch at line 52 is selected by the ITCH contract; all other contracts fall into canonical files. Add an explicit archive source descriptor/branch so BASIC never reaches canonical EQUS files. Preserve raw/normalized identity and checksum checks. Retain volume long enough to apply the new observation policy, then expose the same downstream price interface plus exclusion/provenance metadata. |
| [Price identity](C:/dev/ducketz/ml/stock_target_prices.py:141) | Missing historical identity defaults to canonical EQUS.MINI. Preserve that legacy behavior and reject BASIC-contract/ITCH-dataset or mixed-source combinations. |
| [History cursor validation](C:/dev/ducketz/ml/stock_target_history.py:38) and [manifest construction](C:/dev/ducketz/ml/stock_target_history.py:76) | Dataset, archive root, requests, and symbol cursor checks are hard-coded to ITCH. Parameterize through the explicit source descriptor while retaining all seven symbols and identical session boundaries. |
| [History owner and CLI](C:/dev/ducketz/ml/stock_target_history.py:312) | Catalog and fallback routing are ITCH-only; CLI does not accept a source. Add source plumbing, bind source/policy into history receipts, and gate BASIC Historical completion without invoking ITCH replay. Existing invocation must retain its meaning. |
| [Native cursor monotonicity](C:/dev/ducketz/datafetching/databento_cold_start.py:2210) | Historical backfills preserve a later native stock cursor only for ITCH OHLCV1m. Extend this narrowly to any newly supported independent native stock archive so a BASIC baseline cannot rewind BASIC recurring completion. Do not change unrelated datasets or canonical archive defaults. |
| [Backfill tool](C:/dev/ducketz/ml/stock_target_backfill.py:30) | Dataset and saved-plan identity are ITCH-specific. Either keep it explicitly ITCH-only and introduce a bounded source-aware bootstrap path, or parameterize its immutable plan/resume checks. Never reinterpret an existing ITCH backfill plan as BASIC. |
| [Overnight source choices](C:/dev/ducketz/ml/overnight_runtime.py:44), [resume](C:/dev/ducketz/ml/overnight_runtime.py:212), [stage selection](C:/dev/ducketz/ml/overnight_runtime.py:234), [history command](C:/dev/ducketz/ml/overnight_runtime.py:354) | Runtime duplicates the two-source list; history is omitted unless exact ITCH string matches; command passes no source. Use the registry, include the history stage for supported native contracts, pass the selected source explicitly, and preserve source, successful stages, pinned Gameplan, narrower old stage orders, and original deadline on resume. |
| [Pinned publication checks](C:/dev/ducketz/ml/overnight_runtime.py:160) | Pinning already checks source identity. Add BASIC positive and mismatch tests; no source change within an existing attempt. |
| [Gameplan generation](C:/dev/ducketz/ml/nightly_gameplan.py:131), [manifest configuration](C:/dev/ducketz/ml/nightly_gameplan.py:357), [CLI](C:/dev/ducketz/ml/nightly_gameplan.py:2316) | Source/dataset already flow through target generation and CLI registry. Bind eligible-view policy/exclusions into new manifests and cohorts. New source requires new fits and original development/assessment gates, not relabeling ITCH models. |
| [Independent targets](C:/dev/ducketz/ml/independent_stock_targets.py:137) | Source identity is attached to labels; keep five-minute observations, chronological windows, symbol/route support, and target definition unchanged unless explicitly selected otherwise. Exclude quarantined source intervals consistently. |
| [Source-specific champions](C:/dev/ducketz/ml/gameplan_champions.py:25) | Already compares configuration/model source and dataset. Verify BASIC cannot inherit ITCH champions or stale source-policy fits. |
| [Immutable publication verification](C:/dev/ducketz/ml/nightly_gameplan.py:2172) and [current pointer](C:/dev/ducketz/ml/nightly_gameplan.py:2202) | Preserve old manifests/readability. Add new policy fields compatibly and verify required new fields only for the new contract. |
| [Cumulative evaluation](C:/dev/ducketz/ml/gameplan_evaluation.py:100), [saved sources](C:/dev/ducketz/ml/gameplan_evaluation.py:227), [per-source loading](C:/dev/ducketz/ml/gameplan_evaluation.py:257) | Existing evaluator groups by target family and price identity and loads each saved source independently. Regression-test mixed saved EQUS/ITCH/BASIC publications and each run's own symbol manifest. No BASIC repair of an ITCH outcome. |
| [Planning loader](C:/dev/ducketz/ml/gameplan_trade_planning.py:324), [bands](C:/dev/ducketz/ml/gameplan_price_bands.py:55), [completion](C:/dev/ducketz/ml/gameplan_price_completion.py:32) | Existing source identity is generic. Feed only the selected eligible view, bind source policy into the reusable path, and ensure complete-request evidence does not treat a quarantined interval as trusted. Carry-forward remains the explicit planning-only trailing-anchor exception. |
| [Actuals loader](C:/dev/ducketz/ml/gameplan_actuals_review.py:464) | It correctly loads the original publication's source, independently of the successor. Preserve that behavior. New exclusion/quality diagnostics must distinguish source quarantine from missing in a complete accepted interval. |
| [Enrichment training](C:/dev/ducketz/ml/stock_trader/independent_training.py:112), [model receipts](C:/dev/ducketz/ml/stock_trader/independent_training.py:509), [scope qualification](C:/dev/ducketz/ml/stock_trader/scope_qualification.py:62) | Existing source mismatches fail closed. Require BASIC-bound immutable cohorts, separate fits/qualification, and no passing labels from excluded intervals. |
| [Signal readers](C:/dev/ducketz/ml/stock_trader/independent_signals.py:157), [session readers](C:/dev/ducketz/ml/stock_trader/independent_session.py:33), [fixed budget identity](C:/dev/ducketz/ml/stock_trader/fixed_horizon_budget.py:79), [enrichment readiness](C:/dev/ducketz/ml/stock_trader/model.py:446) | These consumers resolve or compare source identity. Add compatibility/mismatch tests only; avoid changing order sizing, controls, policy selection, cash, inventory, or order submission behavior. |
| [UI publication consumer](C:/dev/ducketz/app/ui/rolling_forecast_data.py:397) and [Gameplan execution reader](C:/dev/ducketz/ml/gameplan_executor.py:47) | They read verified publications. Confirm new provenance displays correctly and old publications still render; there is no reason to modify execution authority. |

The optional strategy runtime and strategy-profit runtime have no direct stock
price-source contract consumer found in the bounded search. They retain their own
publications. Independent stock-only preparation continues to omit them; do not
switch global `DEFAULT_EQUITY_ARCHIVE_DATASET`, provider environment defaults, or
Loop A's canonical bridge to make this one feature work.

## Quality quarantine and zero volume

The 120-session probe records August 31, 2026 as degraded in
[provider-conditions.json](C:/dev/ducketz/artifacts/analysis/cost-recurrence-20260913/source-probe/validation-120/provider-conditions.json:586).
Preserve the exact native provider condition, receipt, scope, and date. The new
source should quarantine that session from model labels and planning samples until
separately verified recovery, rather than silently treating a successful download
as quality acceptance. Exclude training targets crossing the affected interval;
retain counts and reasons. Planning session-pairs touching it must be excluded.
If the current reference or actual boundary depends on it, surface a quality gap
and retain the existing deadline; do not fill it or substitute another dataset.

Acquisition completeness and observation quality are separate. Raw partitions can
be preserved as acquired while the derived quality inventory marks an interval
unqualified. Shared coverage diagnostics and planning-completion evidence must
consider those exclusions, otherwise a full request interval could incorrectly
authorize a synthetic planning anchor over degraded data.

Positive-volume filtering is a candidate policy, not a claim that all zero-volume
provider rows are false or that positive volume independently proves provider
accuracy. Keep the native 0 values and compare both views. Changing acceptance
semantics later requires a new policy version; it must not mutate saved results.

## Specific verification before any prospective activation

1. Extend `test_stock_target_prices.py`: separate BASIC and ITCH archives, exact
   symbol/dataset mismatch, raw tampering, conflicting bars, native zero-volume
   retention versus eligible view, and degraded-day exclusion/provenance.
2. Extend `test_stock_target_history.py`, `test_stock_target_history_fallback.py`,
   and `test_stock_target_backfill.py`: zero-dollar preflight/capacity enforcement,
   per-dataset monotonic cursors, DST exclusive UTC end, no BASIC-to-ITCH fallback,
   unchanged legacy source defaults, and resumed-plan source mismatch rejection.
3. Extend `test_overnight_supervision.py`: BASIC history command/stage inclusion,
   exact pinning, successful-stage retention, unchanged original deadline and
   narrower old stage order, default ITCH scheduled command unchanged.
4. Extend `test_stock_only_gameplan.py`, `test_independent_stock_targets.py`,
   `test_gameplan_champions.py`, and `test_independent_stock_enrichment.py`: all
   168 rows/cohorts source-bound, quality exclusions reduce actual support, gates
   remain unchanged, and ITCH models never qualify a BASIC publication.
5. Extend `test_gameplan_evaluation.py`, `test_gameplan_actuals_review.py`,
   `test_gameplan_price_bands.py`, `test_gameplan_price_completion.py`, and
   `test_gameplan_trade_planning.py`: original-source actuals with a BASIC
   successor, mixed immutable history, no per-gap source blending, six-minute
   tolerance-window semantics, quarantine cannot claim coverage or permit fill,
   and cash/share conservation unaffected.
6. Extend `test_independent_stock_signals.py`, `test_independent_stock_session.py`,
   `test_stock_scope_qualification.py`, and existing forecast/UI tests: source
   support is recognized while mismatches fail closed; no trading actions run.

Only after the source-policy decision and native integration checks should a fresh
prospective experiment fit/assess models. A 120-session price-coverage improvement
alone does not establish directional promotion or sizing qualification.
