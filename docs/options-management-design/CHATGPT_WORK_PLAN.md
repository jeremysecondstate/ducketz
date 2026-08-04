# ChatGPT Work plan: Duckets option position management

This plan converts the four approved concepts into small, mergeable ChatGPT Work tasks. Each workload is intended to become one focused branch or pull request with its own tests.

## Why this split

The visual work is only the top layer. The current application already has useful building blocks:

- `app/ui/options_strategies.py` is the existing 774-line Tkinter strategy-discovery and entry screen.
- `app/ui/ducket_bucket.py` contains the existing Schwab holdings, basic tickets, orders, cancellation, and option-chain UI inside a much larger module.
- `app/services/schwab.py` already reads accounts, positions, orders, quotes, and chains, and can submit or cancel orders.
- `app/services/schwab_policy_inputs.py` already normalizes exact option identity and multi-leg working-order structures.
- `app/services/schwab_strategy_orders.py` already builds entry-order drafts for strategy candidates.

The new implementation should reuse these facts without letting Tkinter widgets become the source of trading semantics.

## Non-negotiable shared rules

Every workload must follow these rules:

1. Do not place a real Schwab order while developing or testing. Use injected fakes and fixtures.
2. Do not infer a broker payload from a Thinkorswim screenshot. Only implement order shapes verified against current official Schwab Trader API documentation.
3. Fail closed when exact option identity, quantity, account state, quote freshness, or broker capability is unavailable.
4. Keep domain calculations and payload builders independent from Tkinter.
5. Preserve exact OCC symbols and signed quantities. Never reconstruct a contract from display text when the broker symbol is available.
6. Never automatically group ambiguous legs into a strategy. Show them as ungrouped until there is deterministic provenance or a user-confirmed grouping.
7. A position is **closed, rolled, or exercised**. A working order is **canceled or replaced**.
8. No management builder may submit directly. All routes end in the universal review surface.
9. Keep the existing Discover/strategy-entry behavior working throughout the migration.
10. Add focused tests for every new pure model, parser, calculation, payload, controller, and safety gate.

## Target package shape

This is guidance rather than a rigid requirement, but it keeps future workloads from returning to monolithic UI modules.

```text
app/
  models/
    option_management.py
  services/
    schwab_option_positions.py
    option_position_grouping.py
    option_management_analytics.py
    schwab_option_management_orders.py
    option_exit_templates.py
  ui/
    options_management/
      container.py
      discover_view.py
      positions_view.py
      orders_view.py
      roll_view.py
      exit_plan_view.py
      review_dialog.py
      widgets.py
```

Keep `OptionsStrategiesTab` as a stable compatibility entry point even if its implementation delegates into this package.

## Workload sequence

| ID | Workload | Depends on | Milestone |
|---|---|---|---|
| 01 | Broker capability and safety contract | — | Foundation |
| 02 | Option-position read model and safe grouping | 01 | Foundation |
| 03 | Quotes, summaries, payoff, Greeks, and impact math | 02 | Foundation |
| 04 | Closing-order draft and payload engine | 01–03 | P0 |
| 05 | Options Strategies navigation shell and UI primitives | 01 | P0 |
| 06 | Options Command Center positions UI | 02, 03, 05 | P0 |
| 07 | Universal review and close execution | 04, 06 | P0 |
| 08 | Orders workspace with cancel/replace | 01, 02, 05, 07 | P0 complete |
| 09 | Roll-order domain engine | 01–04 | P1 |
| 10 | Roll workspace UI and review integration | 03, 07, 09 | P1 |
| 11 | Exit-plan graph and template engine | 01–04 | P1 |
| 12 | Exit-plan UI and broker integration | 06–08, 11 | P1 complete |
| 13 | Exercise workflow | 01–03, 07 | P2 |
| 14 | End-to-end hardening and legacy migration | All prior | Release candidate |

After workload 01, workloads 02 and 05 can safely run in parallel in separate worktrees. Avoid running two later UI workloads in parallel because they will touch the same container and shared widgets.

## Delivery checkpoints

- **P0:** Users can see exact held option positions, close an entire confirmed strategy or selected legs, review the effect, place the closing order safely, and cancel or replace working orders.
- **P1:** Users can roll positions and build supported linked exit plans from reusable templates.
- **P2:** Eligible exercise flows and advanced polish are complete.

---

## Workload 01 prompt — broker capability and safety contract

```text
Work in the Duckets repository at C:\dev\ducketz.

Goal: establish an evidence-backed Schwab option-management capability contract before any new order UI or payload code is written.

Read first:
- docs/options-management-design/README.md
- docs/options-management-design/CHATGPT_WORK_PLAN.md
- docs/schwab/schwab-api-endpoints.md
- docs/schwab/schwab-retail-endpoints.txt
- app/services/schwab.py
- app/services/schwab_strategy_orders.py
- app/services/schwab_policy_inputs.py

Scope:
1. Verify current official Schwab Trader API support for: account positions, individual order lookup, order preview, place, cancel, replace, multi-leg closing orders, net debit/credit, custom strategy orders, child order strategies, OCO, first-triggers-OCO, stop/stop-limit option orders, trailing stops, partial fills, and exercise/assignment requests.
2. Record each feature as VERIFIED, UNSUPPORTED, or UNKNOWN. If official documentation is unavailable or ambiguous, use UNKNOWN; do not infer support from Thinkorswim UI.
3. Document the exact endpoint, HTTP method, required order fields, relevant enum values, response/preview fields, and important restrictions for every verified feature.
4. Define the safety contract shared by later workloads: exact-contract identity, quantity bounds, stale-quote behavior, position-drift behavior, preview behavior, partial-fill handling, and explicit confirmation requirements.
5. Create sanitized JSON fixtures for every verified order shape needed by P0. Fixtures must not contain credentials or real account identifiers.

Deliverables:
- docs/schwab/option-management-capability-matrix.md
- docs/options-management-design/option-management-contract.md
- focused fixtures under tests/fixtures/option_management/
- fixture/schema tests if useful; no production trading implementation

Acceptance criteria:
- Every planned action in the four mockups maps to a verified capability, an explicit fallback, or a disabled/unknown state.
- Close, cancel, replace, roll, OCO, trailing stop, and exercise are treated as separate capabilities.
- The contract explains what happens when preview is unavailable.
- No live API request is made by tests.

Run focused tests plus the full existing pytest suite. Finish with a concise summary of evidence, changed files, test results, and unresolved UNKNOWN capabilities. Do not implement later workloads.
```

## Workload 02 prompt — option-position read model and safe grouping

```text
Work in C:\dev\ducketz. Implement only the normalized option-position read model and deterministic grouping layer.

Read first:
- docs/options-management-design/README.md
- docs/options-management-design/CHATGPT_WORK_PLAN.md
- docs/options-management-design/option-management-contract.md
- app/models/portfolio.py
- app/services/schwab_policy_inputs.py
- app/services/schwab_strategy_orders.py
- tests/fixtures/schwab_account_and_orders.json

Goal: convert the existing normalized Schwab account facts into immutable, UI-independent models for option legs, confirmed strategy groups, ungrouped legs, and working option orders.

Requirements:
1. Consume `PortfolioSnapshot.account_facts` / normalized policy inputs rather than reparsing raw Schwab payloads in the UI.
2. Preserve account identity, OCC symbol, underlying, call/put, expiration, strike, signed net quantity, settled quantity, multiplier, mark/price, market value, cost basis, unrealized/day P/L, Delta, timestamps, source references, and unavailable reasons.
3. Define explicit completeness and capability flags used to enable or disable Close, Roll, Exit Plan, and Exercise.
4. Implement a fail-closed grouping policy. Accept broker/order provenance or a versioned, atomically persisted user-confirmed grouping. Automatically recognize a standard strategy only when the combination is unique and unambiguous. Otherwise return ungrouped legs; never guess.
5. Preserve ratios and support long and short legs. Zero-quantity rows must not appear as open positions.
6. Normalize working orders into a view model that retains all legs, remaining quantity, status, order ID, type, limit/stop price, duration, entered time, and linkage metadata when present.

Suggested modules:
- app/models/option_management.py
- app/services/schwab_option_positions.py
- app/services/option_position_grouping.py

Tests must cover: single long option, single short option, vertical, multi-expiration diagonal, unequal ratios, ambiguous overlapping legs, incomplete OCC identity, missing multiplier, partial working order, persistence round-trip/migration, and a user-confirmed custom group.

Do not build Tkinter UI, broker payloads, or live calls. Run focused tests and the full suite. Report changed files, model contracts, grouping decisions, and tests.
```

## Workload 03 prompt — management analytics

```text
Work in C:\dev\ducketz. Implement only pure option-management analytics used by the four approved UI concepts.

Read the design brief, work plan, option-management contract, and the models produced by workload 02.

Goal: provide deterministic, UI-independent calculations for command-center summaries, price rails, payoff comparison, Greeks aggregation, and clearly labeled impact estimates.

Requirements:
1. Aggregate market value, open/day P/L, Delta, Theta, and protected quantity without treating unavailable data as zero.
2. Provide bid/mid/ask and quote-age models with explicit stale/unavailable states.
3. Calculate expiration payoff for arbitrary signed option legs using exact strikes, quantities, call/put, and multipliers.
4. Calculate bounded max profit/loss and breakevens when mathematically determinable. Represent unbounded or unavailable values explicitly.
5. Compare before/after legs for Roll and Exercise previews.
6. Define an `ImpactEstimate`-style model that can merge local estimates with verified broker-preview fields while preserving provenance. Never label a local approximation as a broker value.
7. Avoid false precision for buying power, fees, settlement, or realized P/L when required facts are absent.

Suggested module: app/services/option_management_analytics.py.

Tests must include long calls/puts, short calls/puts, debit and credit verticals, multi-leg custom positions, ratio spreads, missing Greeks, stale quotes, zero-width markets, unbounded loss, and before/after comparisons.

No Tkinter, payload generation, or network calls. Run focused tests and the full suite. Report formulas, unavailable-state behavior, files changed, and test results.
```

## Workload 04 prompt — closing-order draft and payload engine

```text
Work in C:\dev\ducketz. Implement the pure closing-order engine; do not build UI or submit orders.

Read the capability matrix, option-management contract, workload-02 models, workload-03 analytics, and existing app/services/schwab_strategy_orders.py.

Goal: turn a confirmed option strategy or explicit selected legs into a validated closing-order draft and, only for verified Schwab shapes, a broker payload.

Requirements:
1. Long quantity closes with SELL_TO_CLOSE; short quantity closes with BUY_TO_CLOSE.
2. Bound requested quantities to the latest held quantities and preserve strategy ratios.
3. Support entire-strategy and selected-leg scope. Partial strategy closes must be labeled clearly and revalidated.
4. Preserve exact OCC symbols; never synthesize them from display fields.
5. Model market, limit, net debit, and net credit only where the capability matrix verifies them.
6. Calculate suggested price from fresh leg quotes with explicit source and timestamp. Reject stale or incomplete quotes according to the shared contract.
7. Produce a review-ready draft containing exact legs, action, quantity, complex strategy type, duration, estimated proceeds/cost, warnings, and before/after position state.
8. Fail closed for ambiguous groups, mixed accounts, over-closing, unsupported order shapes, or position drift.

Suggested module: app/services/schwab_option_management_orders.py. Reuse general primitives from schwab_strategy_orders.py where appropriate instead of duplicating them.

Tests must assert exact payload dictionaries for every verified close shape and assert rejection for over-close, wrong action, stale quote, incomplete symbol, unsupported custom order, and changed position quantity.

No Tkinter and no live HTTP. Run focused tests and the full suite. Report files, payload coverage, fail-closed cases, and test results.
```

## Workload 05 prompt — navigation shell and shared UI primitives

```text
Work in C:\dev\ducketz. Refactor the Options Strategies UI shell without changing trading semantics.

Read:
- all four concept images and docs/options-management-design/README.md
- docs/options-management-design/option-management-contract.md
- app/ui/options_strategies.py
- app/ui/ducket_bucket.py
- app/ui/theme.py
- tests/test_options_strategy_ui.py

Goal: create the secondary navigation and reusable Tkinter/ttk components needed by later workloads while preserving the existing strategy-ranking screen as Discover.

Requirements:
1. Keep `OptionsStrategiesTab` as the stable public entry point used by app/ui/ducket_bucket.py.
2. Add internal views for Discover, Positions, Orders, and Templates. Discover must retain all current loading, selection, order-draft, confirmation, and submission behavior.
3. Extract the existing Discover implementation from the 774-line module enough that later views do not enlarge one monolith.
4. Add reusable primitives for summary cards, financial-value styling, segmented actions, empty/loading/error states, section headers, scrollable tables, drawers or panels, and sticky action bars.
5. Extend theme tokens only where necessary; preserve the Duckets dark navy visual language.
6. Use injected callbacks/providers so widgets do not construct broker payloads.
7. Provide stable keyboard focus order and sensible behavior at the current 1900x1000-ish desktop layout.

Positions, Orders, and Templates may be honest placeholders in this workload. Do not implement their business logic or live actions.

Add tests around navigation/controller state and verify existing Options Strategies tests still pass. Run focused tests and the full suite. Report the new package structure, preserved behavior, visual deviations, and tests.
```

## Workload 06 prompt — Options Command Center UI

```text
Work in C:\dev\ducketz. Implement the read-only and draft-selection portions of Concept A.

Read concept-a-options-command-center.png, the design brief, option-management contract, workload-02 position models, workload-03 analytics, and workload-05 UI package.

Goal: make Positions the lifecycle home for held options without adding direct order placement.

Requirements:
1. Render summary cards, account/symbol/expiration filters, group-by-strategy control, grouped and ungrouped positions, exact expandable legs, P/L, DTE, mark, Delta, and action availability.
2. Selecting a confirmed strategy opens a management drawer with Close, Roll, Exit Plan, and Exercise actions.
3. Close supports Entire strategy and Selected legs and renders the reversed-leg preview from workload 04.
4. Actions must be disabled with a concise reason when identity, grouping, quantity, quote, capability, or account state is incomplete.
5. Render missing values as unavailable, not zero. Surface source freshness and refresh failures without discarding the last valid snapshot.
6. Route `Review closing order` to an injected callback/placeholder; do not submit.
7. Do not automatically group ambiguous legs. Make the ungrouped state useful, allow explicit leg selection within the shared contract, and provide a deliberate `Save custom group` route backed by workload 02's grouping store.

Keep data loading and transformations outside the widgets. Add presenter/controller tests for filtering, selection, expanded legs, disabled actions, stale data, and review routing. Preserve Discover behavior and run the full suite.
```

## Workload 07 prompt — universal review and safe close execution

```text
Work in C:\dev\ducketz. Implement Concept D as the universal review surface and connect only the P0 Close route to safe execution.

Read concept-d-order-review.png, the option-management contract, capability matrix, close engine from workload 04, Positions UI from workload 06, app/services/schwab.py, and app/ui/schwab_order_messages.py.

Goal: provide one review dialog/view model that shows exactly what will change and prevents accidental or stale submission.

Requirements:
1. Show account, strategy, instruction, every exact leg, quantity, bid/ask/mark, quote age, order type, TIF, price rail, proceeds/cost, fees, buying-power impact, Delta/Theta change, warnings, and data provenance when available.
2. Re-read the position immediately before placement and compare OCC symbols and quantities with the reviewed draft. Any drift invalidates review.
3. Refresh or invalidate stale quotes according to the shared contract.
4. Use the verified Schwab preview endpoint when supported. If not supported, clearly distinguish local estimates and follow the contract's fallback.
5. Require explicit acknowledgment and a separate Place closing order action. Closing the dialog, Back to edit, validation failure, or rejected preview must never submit.
6. Inject the broker adapter. Unit tests must use a fake that records calls.
7. Handle accepted, rejected, timeout, unknown result, authentication, and network failures without claiming success. Refresh positions/orders after an accepted placement.
8. Generalize the review view model enough for later Roll, Exit Plan, and Exercise routes, but implement live placement only for Close now.

Tests must prove no submission on cancel, unchecked acknowledgment, stale quote, position drift, preview rejection, or unsupported capability; exactly one submission on a valid confirmed path; and safe handling of an unknown network result.

Run focused tests and the full suite. Report all safety gates and test results. Never hit the live Schwab API.
```

## Workload 08 prompt — Orders workspace with cancel/replace

```text
Work in C:\dev\ducketz. Implement the Options Strategies > Orders workspace and retire manual order-ID cancellation from the primary workflow.

Read the capability matrix, option-management contract, normalized working-order model from workload 02, universal review patterns from workload 07, and the current order UI in app/ui/ducket_bucket.py.

Goal: let users inspect exact working option orders and deliberately cancel or replace the selected order.

Requirements:
1. Render Working and Recent orders with order ID, status, entered time, strategy/type, all legs, filled/remaining quantity, limit/stop price, TIF, and linkage.
2. Select the actual order object; never require the user to copy/paste an ID.
3. Cancel uses an explicit confirmation containing the selected order and refreshes state afterward.
4. Replace is enabled only if the current capability matrix verifies the endpoint and order shape. Preserve exact legs and remaining quantity; clearly show changed fields.
5. If native replace is unavailable, do not silently emulate it. Any cancel-then-new-order fallback must be a separately designed two-step flow that explains the exposure gap and requires explicit confirmation; otherwise keep Replace disabled.
6. Correctly handle partial fills, terminal status races, cancellation pending, rejection, and already-canceled orders.
7. Use injected services and fake broker tests. No live network calls.

Add controller tests proving that only the selected current order can be canceled/replaced, terminal orders cannot be mutated, and refresh races are safe. Preserve the Schwab Duckets screen for now; cleanup happens in workload 14. Run the full suite and report behavior and tests.
```

## Workload 09 prompt — roll-order domain engine

```text
Work in C:\dev\ducketz. Implement only the domain engine for rolling option positions.

Read the capability matrix, option-management contract, position/group models, analytics, and closing-order engine. Do not build Tkinter UI or submit orders.

Goal: model a roll as an explicit close of current legs plus an explicit open of replacement legs, with verified atomicity semantics and before/after analysis.

Requirements:
1. Support Entire strategy and Selected legs.
2. Preserve exact current OCC symbols and accept exact replacement-chain contracts; do not synthesize replacement OCC symbols.
3. Support keep-strike-width and days-forward helpers as suggestions only. Always return explicit replacement legs for review.
4. Validate expiration ordering, call/put consistency, quantities, ratios, account, buying-power evidence, and quote freshness.
5. Calculate one net debit/credit when the verified API supports an atomic custom roll. If the broker requires separate components, model each component and the non-atomic risk explicitly; never pretend atomicity.
6. Produce before/after payoff, Greeks, realized-P/L estimate, buying-power effect, days extended, fees, warnings, and a review-ready draft.
7. Fail closed for ambiguous grouping, unsupported compound shape, missing quotes, or position drift.

Tests must include single-leg rolls, vertical rolls, diagonal/calendar behavior, selected-leg rolls, ratio preservation, debit and credit rolls, unsupported atomicity, and changed positions.

No UI or live calls. Run focused tests and the full suite. Report the roll state model, verified payload shapes, explicit non-atomic cases, and tests.
```

## Workload 10 prompt — roll workspace UI

```text
Work in C:\dev\ducketz. Implement Concept B using the roll engine from workload 09 and universal review from workload 07.

Read concept-b-roll-workspace.png and all relevant contracts/models.

Goal: create the Configure → Analyze → Review roll workflow without duplicating order or calculation logic in Tkinter.

Requirements:
1. Show Position to close, the Roll to connector, Replacement position, and Net order as distinct sections.
2. Support expiration selection from exact option-chain results, keep-strike-width suggestions, individual leg enablement only when valid, limit-price controls, and bid/mid/ask.
3. Render practical before/after payoff and Greeks views using workload-03 data. Do not invent values when missing.
4. Show realized loss, buying-power change, days extended, fees, and atomic/non-atomic warnings before review.
5. Route to the universal review view model. Place only through its confirmed safety gates.
6. Save as template may store configuration defaults but must not store stale quotes, account balances, or executable OCC symbols as timeless assumptions.
7. Handle chain-loading errors, expired selections, quote changes, and position drift.

Add presenter/controller tests for leg mapping, expiry changes, keep-width behavior, net-price changes, unavailable analytics, non-atomic warnings, and review routing. Use fake services only. Run the full suite and report implementation and tests.
```

## Workload 11 prompt — exit-plan graph and template engine

```text
Work in C:\dev\ducketz. Implement only the domain model, validation, persistence, and verified broker conversion for linked exit plans.

Read the capability matrix, option-management contract, close engine, and concept-c-exit-plan-builder.png. Do not build UI or submit orders.

Goal: represent Target + stop, Single target, 2 targets, Trailing stop, and optional time-based exits as a versioned order graph rather than ad hoc widget state.

Requirements:
1. Model position coverage, trigger basis, trigger operator/value, order type, limit offset, quantity, TIF, OCO/link relationships, activation rules, and enabled state.
2. Validate that linked exits cannot collectively over-close the position and that quantities remain synchronized where required.
3. Convert only verified graph shapes to Schwab payloads. Unsupported templates remain representable but non-executable with a precise capability reason.
4. Detect conflicts with existing working closing orders for the same legs.
5. Persist reusable templates in a versioned, atomic, human-readable local format. Templates may store percentages and policy defaults, but not credentials, account IDs, stale quotes, or blindly reusable position quantities.
6. Rehydrate templates safely across schema versions and reject malformed or unsafe files.
7. Produce review-ready drafts and warnings, including stop-limit gap/fill risk.

Suggested module: app/services/option_exit_templates.py plus order-graph models in app/models/option_management.py.

Tests must cover every template type, malformed templates, over-close, conflicting orders, unsupported linkage, partial fills, percentage-to-price resolution, rounding, and persistence migration.

No Tkinter or live network. Run focused tests and the full suite. Report graph semantics, executable capability coverage, persistence path/version, and tests.
```

## Workload 12 prompt — exit-plan UI and integration

```text
Work in C:\dev\ducketz. Implement Concept C using the exit-plan engine from workload 11, Positions UI, Orders state, and universal review.

Read concept-c-exit-plan-builder.png and the shared contracts.

Goal: provide a visually obvious, safe bracket/OCO exit-plan builder for an entire multi-leg strategy or explicit selected legs.

Requirements:
1. Render template cards, position coverage, exact leg chips, linked exits, OCO relationship, trigger controls, optional time exit, sequence preview, at-a-glance values, safeguards, warning rail, and review action.
2. Keep unsupported templates visible but disabled with a capability explanation; do not fake broker support.
3. Make the OCO relationship unmistakable in both editor and sequence preview.
4. Resolve percentages against a fresh position mark and clearly show the resulting target/stop prices and rounding.
5. Detect active closing orders and block or reconcile overlap according to the shared contract.
6. `Close now…` routes into the existing Close builder/review; it is not a one-click submit. `Cancel working orders` routes to the Orders confirmation flow.
7. Route executable plans through universal review and its placement safety gates.

Add controller tests for template selection, synchronization, unsupported states, mark changes, conflicting working orders, OCO visualization state, close/cancel routing, and review routing. Use fakes only. Run the full suite and report capability gaps and tests.
```

## Workload 13 prompt — exercise workflow

```text
Work in C:\dev\ducketz. Implement the Exercise action conservatively and only to the extent verified by the capability matrix.

Read the capability matrix, option-management contract, option-position models, analytics, and universal review implementation.

Goal: give eligible long option legs an explicit exercise analysis/review flow without inventing a broker endpoint.

Requirements:
1. Eligibility is per exact long leg, never an entire mixed strategy by default.
2. Show contract, quantity, expiration, intrinsic/extrinsic value when available, resulting stock quantity/cash effect, settlement, buying-power impact, dividend/early-exercise caveats where supported by facts, and assignment interactions with other legs.
3. Reject short legs, over-quantity, incomplete identity, expired/invalid states, and position drift.
4. If Schwab API exercise submission is VERIFIED, implement it behind the universal review and fake-tested broker adapter. If UNSUPPORTED or UNKNOWN, implement the analysis UI as disabled/read-only with an accurate explanation and no fabricated call.
5. Never describe exercise as closing the option through a market order.

Tests must cover eligible long call/put, short leg, multi-leg position, quantity bounds, missing intrinsic data, position drift, unsupported capability, and confirmed submission only if verified.

No live API calls. Run focused tests and the full suite. Report whether the result is executable or analysis-only and cite the capability-matrix decision.
```

## Workload 14 prompt — end-to-end hardening and legacy migration

```text
Work in C:\dev\ducketz after workloads 01–13 are merged. Do not add new product features.

Goal: make the option-management lifecycle release-ready, remove duplicate primary workflows safely, and prove fail-closed behavior end to end.

Scope:
1. Build an injected fake Schwab adapter and scenario fixtures covering long/short single legs, verticals, custom multi-leg groups, ambiguous legs, partial fills, stale quotes, incomplete account reads, network failures, preview rejection, unknown submit result, position drift, and linked orders.
2. Add integration tests for Position → Close → Review → Place, order cancel/replace, Roll → Review, Exit Plan → Review, and Exercise when executable.
3. Verify exactly-once submission behavior and that repeated clicks, refresh races, tab changes, or dialog close events cannot duplicate orders.
4. Audit all loading/error/empty/disabled states, keyboard navigation, focus, color-independent status cues, table scrolling, and the target desktop resolution.
5. Ensure network work does not freeze or mutate Tkinter from a worker thread. Preserve the last valid snapshot during refresh failures.
6. Run a visual QA pass against all four concept images and document intentional deviations.
7. After parity is proven, remove or clearly deprecate duplicate option-management controls in Schwab Duckets. Keep account sync and any still-useful low-level account/chain functionality. Do not delete a legacy path without test-backed parity.
8. Split any remaining oversized option-management UI modules where it reduces coupling, without changing behavior.
9. Update README/user documentation and add a concise operator safety checklist.

Run the entire pytest suite. Do not use a live account or submit a real order. Finish with changed files, full test results, remaining capability limitations, legacy controls retained/removed, and a release-readiness checklist.
```

## Definition of done for every Work task

A task is done only when:

- its stated scope is complete and later workloads were not pulled in;
- new behavior is covered by deterministic tests;
- no test can call the live Schwab API;
- existing behavior still passes;
- unavailable and error states are explicit;
- changed files and test results are summarized;
- any contract change is documented for downstream workloads.
