# Forecast/book timing retries — September 26, 2026

At06:15:09 Pacific, the UI showed Qualified ETH/HYPE/ZEC decisions skipped with
`Executable book predates the forecast.` The user requested multiple retry
attempts for these timing failures.

The old tick fetched books before reading latest forecasts. A forecast published
while book requests were in flight could therefore be newer than the selected
book. Rejecting that book was correct; committing the quote-only skip under the
ordinary processed-forecast key prevented a subsequent retry with fresh prices.

Read-only diagnosis at13:22:16 UTC found three affected forecasts and nine
account decisions, zero fills/transfers/funding in each affected cycle, and no
later decisions for those forecast IDs:

| Asset | Forecast publication UTC | P(not-down) |
| --- | --- | ---: |
| ETH | 13:15:09.237247 | 44.13934874947018% |
| HYPE | 13:15:09.309007 | 47.19209222109081% |
| ZEC | 13:15:09.382856 | 42.331216848110825% |

All three passed the49% short-entry threshold. Original skips did not retain the
book timestamp, so the exact timing gap cannot be reconstructed from those rows.
Current and target columns matched because quote rejection replaced the desired
target with current exposure; saved `proposed_target_notional` retains the
earlier requested allocation. Qualification and probability alone do not bypass
account capacity, minimum trade sizes or other remaining checks.

Diagnosis evidence: `C:/DATASTORE/hyperliquid/_operations/paper-quote-retry-20260926-diagnosis.json`.

## Execution correction

Freeze validated forecasts before fetching fresh executable books. Do not reuse
opening seed quotes for trades. Retry timing/missing-book failures up to three
snapshot attempts per tick, with0.1/0.2-second backoff, then keep pure quote-only
attempts pending for subsequent polls while the forecast remains valid. Recheck
the frozen forecast's validity deadline after fetching; never substitute a newer
forecast after choosing books or execute an expired forecast.

Fixed keys separate pending evidence from completion:

- `forecast-wait:<id>` records one deferred diagnostic and does not consume it.
- `forecast:<id>` remains the ordinary completion identity.
- `forecast-retry:<id>` permits one append-only completion of an old quote-only
  skipped cycle, retaining its original rows.

Legacy recovery requires exactly the three expected account decisions, all
explicit quote-error skips, and zero fills, transfers and funding. Completed or
partially executed cycles, mixed outcomes and incomplete records cannot use this
recovery. Indexed targeted lookups avoid scanning whole journals. New decisions
retain book/forecast timing, attempt count, pending and recovery provenance.

Thresholds stay49%/51% for both entry and exit. Fees, zero added slippage,
qualification, risk limits and cooldowns remain. Versioned execution policy is
`forecast_first_bounded_quote_retry_v1`, yielding policy **1d260fb385aec9de**;
configuration hashes are unchanged from the shared-threshold phase. Deployment
verification is recorded below when the same ledger resumes under this policy.

## Deployment and recovered decisions

**330 tests passed** across runtime, ledger, policy and market. Coverage includes
multiple snapshots and backoff, publication during fetch, forecast expiry,
pending/recovery persistence across restart, append-only completion, cooldowns,
and rejection of mixed, partial or otherwise effectful historical cycles.

Old Paper PID48468 stopped gracefully. The same ledger resumed as actual
PID53036, launcher45180, hidden cmd76008, created13:26:49.554817 UTC. Seed
**2026-09-26T12:34:36.903077126Z**, experiment **20260926T123436Z-book-vwap** and
opening **42088.24361973616** remain unchanged. No fresh mirror was taken.

At13:26:53 UTC, all three still-valid affected forecasts completed their fixed
recovery cycles using fresh books:

| Asset | Alex | Jeremy | Clear Pond |
| --- | --- | --- | --- |
| ETH | Filled signal adjustment | Opposite account direction | Opposite account direction |
| HYPE | Filled signal adjustment | Opposite account direction | Below size precision |
| ZEC | Account capacity hold | Opposite account direction | Below size precision |

Three simulated collateral transfers and two fills were appended. This fixes
the timing failure without promising that other gates disappear. Independent
replay from the stopped state verified cash, taker fees, quantities, historical
basis, stop references and marked equity through each new cycle. Old journal
prefixes, old policy files, the seed and opening snapshot hash are unchanged.

At13:28:10.742260 UTC, ledger had advanced to13:27:56.723021 with155 cycles,
14 fills and165 decisions. All17 source states were fresh, no warnings/errors,
data57004/models56520 unchanged, and Powder inactive with zero pending intents.
The watch uses contractv9 and the new execution policy; monetary and threshold
configuration hashes are unchanged. Evidence under `_operations`:

- `paper-quote-retry-20260926-before.json`
- `paper-quote-retry-20260926-after.json`
- `paper-quote-retry-20260926-prior-maintenance.json`
