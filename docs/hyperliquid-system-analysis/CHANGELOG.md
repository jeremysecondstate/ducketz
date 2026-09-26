# Hyperliquid system-reference changelog

This records changes to the maintained system contract. Runtime events and
performance history belong in their own journals and dated evidence.

## 2026-09-25 — Explicit Powder activation path

- Added the separate Powder execution runtime, strict verified mainnet broker,
  durable client-order intent/reconciliation ledger and user launch/check/stop
  commands. Every session requires explicit activation; no scheduler or `.env`
  change was installed.
- Shared validated forecast reading and pure allocation policy with Paper,
  adopted matching existing positions, enforced actual account collateral and
  shared ownership locks with manual submit/modify/cancel operations.
- Added separate read-only Powder observations/intents/actual fills to H.Y.P.E.R.
  Actual fee tokens are preserved; P/L/drawdown/funding totals and automated
  transfers remain unavailable pending complete cashflow implementation.
- Added the [activation/recovery runbook](POWDER_ACTIVATION.md) and updated the
  index, map, inventory, functionality, monitoring, UI and maintenance contracts.
  Test evidence and limitations are recorded in the runbook. Verification uses
  fake exchange responses and temporary ledgers, not live execution.
- Final regression selection: **917 passed** across all Hyperliquid tests and
  both workspace/app integration suites. Normal/compact Powder fixture images
  were inspected; local saved status reported **NOT_ACTIVATED**.

This entry supersedes the earlier disconnected-Powder implementation boundary.
The earlier dated screenshots and initial reference entry remain historical.

## 2026-09-25 — Initial system-analysis reference

- Established this index and operating reference alongside the existing
  stock/options [Loops reference](../loops-system-analysis/README.md).
- Mapped the independent data, model and Paper owners, their publication
  dependencies, the one-time mirror seed, evaluation evidence and read-only UI.
- Documented model qualification versus profitability, Paper opening-baseline
  accounting, transfers, account roles, execution assumptions and risk limits.
- Added process/source/timing diagnostics, control side effects and recovery
  guidance. Kept runtime observations separate from permanent architecture.
- Recorded H.Y.P.E.R. Paper/Powder boundaries and the current fixed UI market
  and horizon scope; Powder remains disconnected.
- Added a change-impact matrix, update checklist, evidence format and existing
  test-family map for future maintenance.

Baseline review used the working-tree implementation/configuration at revision
`f4efaeb` plus the existing component and UI implementation guides. Verification
for this documentation change is source review and local Markdown link/path
checking: 202 local links across the 12 new documents and six linked entry
pages resolved, with balanced fenced code blocks. No worker lifecycle,
strategy, ledger or scheduling change is part of
this entry. Previous UI screenshots and test results remain dated delivery
evidence, not newly measured runtime performance.

For later entries, record the behavior/configuration change, affected pages,
checks actually performed and any unresolved limitation. Do not claim automatic
documentation updates or replace old observations with current numbers.
