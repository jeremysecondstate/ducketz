# Corrected fresh mirror: opening and first trading cycles

Verified **2026-09-26 12:06:35 UTC / 05:06:35 PT**. The user clarified the
required lifecycle: copy the current actual accounts 1:1, then start Paper
trading with realistic holdings/cash accounting, current prices and costs.
Paper was intentionally paused during preparation; data and models continued.
No real orders, transfers or Powder activation occurred.

## What was corrected

The [read-only reconstruction](2026-09-26-paper-restart-reconstruction.md)
established that the disputed opening copied inventory correctly, but seven
immediate fills were already reflected in the first visible equity observation.
Historical inherited losses also triggered two new-experiment stops. Its initial
$40.902009 loss reconciled fully to fees and execution costs, not invented cash.

New mirror creation now commits an immutable `opening` cycle and all four equity
observations with its seed, before any decisions/fills. Initialization publishes
this state to the UI and exports; `--prepare-only` creates it and stops. Resume
opens that same seed and fetches fresh executable books. Reopening/refreshing the
UI does not reseed, rebalance, or control workers.

Perpetual historical `avg_entry` is preserved for accounting. Each inherited
position has a separate persisted opening-mark stop reference. Partial reductions
preserve it, additions weight it with fill price, and close/reopen starts a new
reference. Resuming a legacy ledger preserves its previous entry-based stop
semantics. Actual stop fills still create the one-horizon cooldown. Decision
records show entry, reference, stop return, and threshold.

The UI exposes an immutable opening summary and complete inventory dialog;
chart sampling preserves the opening observation. Existing histories are not
backfilled. Current equity/positions/fees/charts come from the ledger. Headline
worst drawdown is explicitly a dated performance export, whose freshness cadence
now matches the runtime's 900 seconds.

Entry remains 54%/46%, exit retention 52%/48%, Qualified-only, with all account,
symbol and pool caps unchanged. Neither old losses nor costs were erased to
manufacture a result. There is no promise that the strategy preserves inherited
holdings after it starts making decisions.

## Untouched opening, independently verified before resume

Experiment: `20260926T120059Z-fresh-mirror-opening`.
Seed: **2026-09-26T12:00:59.251769781Z / 05:00:59 PT**.
Policy: **6b404ac74a142e9f**, versioning the separate stop reference.
All four configuration file hashes are unchanged from the handoff.

| Account | Opening equity | Opening minus sequential source equity |
| --- | ---: | ---: |
| Alex | $5,848.855170 | -$0.006585 |
| Jeremy | $6,173.877739 | +$0.004615 |
| Clear Pond | $30,077.997935 | $0.000000 |
| Pool | **$42,100.730844** | **-$0.001970** |

The prepared ledger had **nine inherited positions, one opening cycle, four
equity rows, zero fills, zero decisions, zero transfers, zero funding, zero fees,
and $0 experiment P/L**. Gross exposure was $50,962.747757, reflecting all
opposing positions separately. It was not resized to fit strategy limits during
seeding. Initial cash/collateral, signed quantities, retained entries, marks and
account baseline equity reconciled independently.

New public reads retain per-request start/completion times, available exchange
timestamps, and separate quote timing/marks. The account-to-opening differences
above reconcile exactly to perpetual mark P/L minus source-reported P/L. These
are sequential public reads, not an atomic exchange snapshot. No historical
timestamps were invented for the earlier disputed run.

Evidence under `C:/DATASTORE/hyperliquid/_operations/`:

- `paper-restart-20260926-opening-verification.json`
- `paper-restart-20260926-pretrade.png`
- `paper-restart-20260926-opening-inventory.png`

Both UI screenshots were inspected: current and opening equity were $42,100.73,
P/L and fees were $0, all nine positions appeared in the inventory dialog, and
the chart contained its untouched opening point. The prepared status correctly
said it was awaiting strategy start.

## First actual simulated moves and cash/holding accounting

After opening verification, continuous Paper launched at **12:03:41 UTC** and
committed its first complete tick at **12:03:44.055339 UTC**. The maintained
opening remained unchanged. Seven fills executed from fresh public books:

| Account / asset | Signed quantity before → after | Cash/collateral change, including fee | Fee | Reason |
| --- | ---: | ---: | ---: | --- |
| Clear Pond BTC spot | 0.2001388183 → 0.0000088183 | +$16,822.985713 | $11.784339 | Exposure cap |
| Alex HYPE perp | -80 → 0 | -$505.869135 | $3.315759 | Qualified exit |
| Jeremy HYPE perp | 60 → 0 | +$284.574769 | $2.485564 | Qualified exit |
| Clear Pond HYPE spot | 25.04113363 → 0.00113363 | +$2,305.109456 | $1.614707 | Qualified exit |
| Alex ZEC perp | -6.25 → -1.54 | -$926.235318 | $3.269559 | Qualified target reduction |
| Jeremy ZEC perp | 4.5 → 0 | +$699.580466 | $3.122105 | Qualified opposite-side exit |
| Clear Pond ZEC spot | 1.504655136 → 0.000055136 | +$2,315.528552 | $1.622005 | Qualified opposite-side exit |

Spot cash changes equal sale proceeds minus fees (buys debit notional plus fees).
Perpetual collateral changes equal realized signed P/L minus fees; perp notional
is not a spot-style cash debit. Historical P/L realized on inherited positions is
not all new experiment P/L because opening equity already includes it. Quantity,
historical basis, risk reference, fees, collateral, and marked equity were replayed
independently from the seed through **every committed cycle** and matched.

BTC's Research forecast was excluded. Its sale was a separate cap action: the
inherited portfolio exceeded the 60% pool limit and other-symbol gross left BTC
no capacity under the existing sequential calculation. Qualified HYPE
P(not-down)=0.4847847337 lay inside the exit band. Qualified ZEC
P(not-down)=0.4434396252 retained a sized Alex short; it did not trigger a
historical-loss stop. There were **zero stop-loss fills and zero new cooldowns**.
ETH's Qualified 0.4399528621 short was blocked by capacity when processed before
HYPE/ZEC reductions; existing once-per-forecast processing was left unchanged.

The first-tick loss is completely attributable:

| Opening-to-first-tick component | Dollars |
| --- | ---: |
| Opening equity | 42100.730843905156 |
| Market movement between preparation and first executable quotes | -8.091270506760 |
| Raw book prices versus those marks | -2.295810000001 |
| Configured additional 2 bp slippage | -9.711605921999 |
| Simulated fill fees | -27.214037895307 |
| Funding / net transfers | 0 |
| First complete tick equity | **42053.41811958109** |
| Experiment P/L | **-47.312724324066** |

This preparation-to-trading market movement was preserved; prices were not held
fixed during verification. First-tick exposure was $2,375.923066, including the
remaining **-1.54 ZEC** and precision dust. The model/cap policy still produces a
substantial initial reallocation, now with a visible untouched origin and no
pre-experiment stop attribution.

First-tick evidence is preserved separately in
`paper-restart-20260926-first-cycle-verification.json`. Later evidence in
`paper-restart-20260926-trading-verification.json` shows advancement to
**12:06:22.853618 UTC**, 11 cycles and still seven fills. Equity $42,050.030341,
P/L -$50.700503, fees $27.214038 and exposure $2,379.311287 all reconcile;
the additional -$3.387779 is subsequent position mark movement.
The verification script `paper-restart-20260926-verify.py` opens SQLite read-only;
its only writes are the separate verification reports.

## Preservation, health and validation

The disputed run was gracefully stopped, then preserved intact under
`_paper_archives/20260926T113032Z-fresh-entry-4pp-disputed`. A manifest in
`paper-restart-20260926-preservation.json` verifies every saved file hash remained
unchanged. Its original `analysis_eligible: true` metadata was preserved as
evidence, not treated as user acceptance. The separately excluded predecessor
was not restored or evaluated; earlier archives and independent model research
remain separate.

At **12:06:35 UTC**, Paper **71908** (venv **58416**, hidden cmd **62936**),
data **57004**, and models **56520** were running with advancing fresh observations.
All 17 projected sources were fresh, with no reported errors/warnings or stop
requests. Powder remained inactive and disconnected with zero pending intents.
Evidence: `paper-restart-20260926-health-verification.json`.

**491 relevant tests passed**, plus the final **80-test focused UI/service run**.
Coverage includes atomic/failed/idempotent opening, public-read provenance,
historical basis versus stop reference, partial/add/full-close/reopen accounting,
legacy resume, prepare-only then resume with new books, real post-opening stops,
Qualified entries/exits, Research/missing holds and independent risk reductions,
chart sampling, UI timing and Paper/Powder separation.

Operations Watch remains active on its existing 30-minute schedule and model.
Contract v6, app prompt and compact memory use this new opening; recovery must
resume it without reseeding. The app tool confirmed the watch ACTIVE with its
unchanged schedule/model; the maintenance record is now completed after both
opening and first-cycle verification. A final read at **12:09:34 UTC** confirmed
Paper 71908 still running without errors/warnings and a newer committed observation
at **12:09:03.093595 UTC**, retaining the same seed. Subsequent market movement had
changed equity to $42,040.482704; fees remained $27.214038.
