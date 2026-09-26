# Disputed Paper restart: opening and first-cycle reconstruction

This is a forensic reconstruction of the active experiment
`20260926T113032Z-fresh-entry-4pp`, not an acceptance of its restart semantics.
The opening inventory and equity reconcile, but seven simulated executions
liquidated essentially all meaningful inherited holdings within 0.66 seconds.
The first-cycle equity loss was **$40.902008521487**, completely explained by
fees and execution prices. Historical-entry stops, Qualified neutral/opposite
signals, and the sequential exposure-cap calculation explain the liquidation.
A correct seed alone did not produce the user-requested observable fresh start.

The investigation made no operational, exchange, configuration, ledger, or
metadata changes. It used `.venv/Scripts/python.exe` and SQLite read-only
connections. This document is the only file written by this reconstruction.
No excluded predecessor was read, restored, reconstructed, or evaluated.

## Evidence scope and reproducibility

Primary evidence:

- `C:/DATASTORE/hyperliquid/_paper/ledger.sqlite3`, including its live SQLite WAL
  through normal read-only SQLite access.
- `C:/DATASTORE/hyperliquid/_paper/opening_snapshot.json`.
- `C:/DATASTORE/hyperliquid/_paper/experiment.json`.
- `C:/DATASTORE/hyperliquid/_paper/performance.json` for the export timestamp.
- Source functions `mirror_accounts` in `ml/hyperliquid_paper_seed.py`,
  `PaperLedger.__init__`, `state`, and `execute_cycle` in
  `ml/hyperliquid_paper_ledger.py`, and `_trade_coin`/`tick` in
  `ml/hyperliquid_paper_runtime.py` as inspected during this investigation.

The earlier verification JSON was read only after independently reconstructing
the opening and fills. Its conclusions were not treated as proof of acceptance.
The current experiment metadata still has `analysis_eligible: true`; that is a
stored flag, not user approval of this disputed comparison baseline. This audit
does not alter that flag or dispose of current-run evidence.

Representative connection and queries:

```python
import json
import sqlite3

connection = sqlite3.connect(
    "file:C:/DATASTORE/hyperliquid/_paper/ledger.sqlite3?mode=ro", uri=True
)
connection.row_factory = sqlite3.Row
connection.execute("PRAGMA query_only=ON")
connection.execute("BEGIN")  # Consistent view while the independent worker runs.
seed = json.loads(connection.execute(
    "SELECT details_json FROM seed WHERE singleton=1"
).fetchone()[0])
fills = list(connection.execute("SELECT * FROM fills ORDER BY rowid"))
cycles = list(connection.execute(
    "SELECT cycle_id,timestamp_utc,result_json FROM cycles ORDER BY timestamp_utc"
))
decisions = list(connection.execute("SELECT * FROM decisions ORDER BY rowid"))
opening_rows = connection.execute(
    "SELECT COUNT(*) FROM equity WHERE timestamp_utc <= ?",
    (seed["timestamp_utc"],),
).fetchone()[0]
connection.close()
```

`seed.details_json` contains the opening cash, signed inventory, marks,
source-account summaries, and baseline equities. `initial_positions` repeats
the opening positions. `fills` records signed quantity, fill price, fee,
historical-cost realized P/L, reason and `details_json`; the details preserve raw
book VWAP, consumed levels, explicit slippage, policy ID and forecast provenance.
`decisions.details_json` preserves the proposed and final targets, actual
thresholds, account role, capacity and cooldown checks. `cycles.result_json`
preserves each committed post-cycle state, including position marks. `equity`
contains account rows plus `account='pooled'`, not `account='pool'`.

The detailed current-state attribution below uses the committed cutoff
**2026-09-26 11:51:46.765475 UTC (04:51:46 PT)**. That consistent read contained
49 cycles, 8 fills, 24 decisions and 196 equity rows, with zero funding and
transfer rows. The independent worker was continuing to append observations;
these numbers are a cutoff, not a claim about the later live state.

## Opening inventory and account reconciliation

Seed timestamp: **2026-09-26T11:30:32.118497133+00:00 (04:30:32 PT)**.
Opening pooled equity: **$42,099.529976985854**. Policy ID:
`993e26589020a5a0`. There were nine inherited positions and no seed fills or
synthetic seed fees.

| Account | Kind | Asset | Signed quantity | Historical entry / opening spot basis | Opening mark |
| --- | --- | --- | ---: | ---: | ---: |
| Jeremy | Perp | HYPE | +60 | 87.2736 | 92.028 |
| Jeremy | Perp | ZEC | +4.5 | 1385.6239 | 1539.6 |
| Jeremy | Spot, passive | HYPE | +0.00000752 | 92.067 | 92.067 |
| Alex | Perp | HYPE | -80 | 85.8225 | 92.028 |
| Alex | Perp | ZEC | -6.25 | 1346.6497 | 1539.6 |
| Alex | Spot, passive | HYPE | +0.00003283 | 92.067 | 92.067 |
| Clear Pond | Spot | HYPE | +25.04113363 | 92.067 | 92.067 |
| Clear Pond | Spot | BTC | +0.2001388183 | 84172 | 84172 |
| Clear Pond | Spot | ZEC | +1.504655136 | 1538.1 | 1538.1 |

Perpetual entries were retained from the account source. Spot historical cost
was unavailable; the explicit recorded basis is `opening_mark_not_historical_cost`.
Alex and Jeremy spot dust was marked passive. Clear Pond's spot holdings were
managed strategy inventory.

For these unified/portfolio-margin accounts, the implementation avoids counting
perpetual P/L twice by setting base collateral to source spot USDC minus source
perpetual unrealized P/L. It does not add the reported perpetual account value a
second time. Paper equity is base collateral plus current perpetual unrealized
P/L plus spot market value.

| Account | Source mode | Source spot USDC | Source perp account value | Source perp unrealized P/L | Paper base collateral |
| --- | --- | ---: | ---: | ---: | ---: |
| Alex | unifiedAccount | 5879.29520537 | 2435.2892 | -1702.29279 | 7581.587995370001 |
| Jeremy | portfolioMargin | 6151.7274009 | 1336.691763 | +978.091835 | 5173.6355659 |
| Clear Pond | unifiedAccount | 8602.66889727 | 0 | 0 | 8602.66889727 |

| Account | Opening spot value | Perp P/L at opening marks | Reconstructed / recorded opening equity | Recorded source equity | Opening minus source |
| --- | ---: | ---: | ---: | ---: | ---: |
| Alex | 0.00302255961 | -1702.379375 | 5879.21164292961 | 5879.29822792961 | -0.086585 |
| Jeremy | 0.00069234384 | +978.15645 | 6151.792708243839 | 6151.72809324384 | +0.064615 |
| Clear Pond | 21465.856728542407 | 0 | 30068.525625812406 | 30068.525625812406 | 0 |
| Pooled | 21465.860443445857 | -724.222925 | 42099.529976985854 | 42099.551946985855 | -0.021970 |

The independent reconstruction agrees with the saved baseline to floating-point
precision. The few-cent source differences are exactly the differences between
source-reported perpetual P/L and P/L recomputed at the common opening marks.
They are not the subsequent approximately $41 loss. Source metadata records zero
unimported open orders for every account.

### Timestamp limitations

The current seed files do **not** retain the raw account API responses or a
start/end timestamp for each account read. The inspected `mirror_accounts`
fetches the three accounts in parallel, but for each account performs
`clearinghouseState`, `spotClearinghouseState`, `userAbstraction` and
`frontendOpenOrders` reads sequentially, then obtains market quotes. It records
one final seed timestamp. Therefore the historical reads cannot now be made
atomic or assigned exact request timestamps from this evidence.

Executable book times *are* retained in fill details: initial perpetual books
were observed at **11:30:31.115 UTC**, spot books at **11:30:31.666 UTC**. First
tick source code uses the seed's quote snapshot; the committed initial cycle
marks match the seed marks. The account metadata explicitly says the snapshot
is not atomic across accounts. Any future correction should preserve request
timing and source valuation provenance instead of claiming exact simultaneity.

## First committed cycles and seven fills

Each forecast below was committed separately, in sorted symbol order BTC, ETH,
HYPE, ZEC. All seven initial fills used saved policy ID `993e26589020a5a0`.
Amounts below retain enough precision to reproduce the dollar totals.

| Cycle commit, UTC | Asset | Fill count | Pooled equity after commit | Experiment P/L | Gross exposure |
| --- | --- | ---: | ---: | ---: | ---: |
| 11:30:32.747721 | BTC | 1 | 42084.17157729877 | -15.358399687084 | 34055.13808344586 |
| 11:30:32.764850 | ETH | 0 | 42084.17157729877 | -15.358399687084 | 34055.13808344586 |
| 11:30:32.771112 | HYPE | 3 | 42072.99206798575 | -26.537909000100 | 18865.86040344586 |
| 11:30:32.776779 | ZEC | 3 | 42058.62796846437 | -40.902008521487 | 0.935143445859 |
| 11:30:32.777969 | Mark observation | 0 | 42058.62796846437 | -40.902008521487 | 0.935143445859 |

| UTC commit | Account | Instrument | Signed fill quantity | Fill price | Fee | Recorded reason |
| --- | --- | --- | ---: | ---: | ---: | --- |
| 11:30:32.747721 | Clear Pond | BTC spot | -0.20013 | 84154.1658 | 11.789241241088 | risk_cap |
| 11:30:32.771112 | Alex | HYPE perp | +80 | 92.05478102475 | 3.313972116891 | stop_loss |
| 11:30:32.771112 | Jeremy | HYPE perp | -60 | 92.0085946 | 2.484232054200 | signal_rebalance |
| 11:30:32.771112 | Clear Pond | HYPE spot | -25.04 | 92.0485866 | 1.613427625925 | signal_rebalance |
| 11:30:32.776779 | Alex | ZEC perp | +6.25 | 1540.00794 | 4.331272331250 | stop_loss |
| 11:30:32.776779 | Jeremy | ZEC perp | -4.5 | 1539.2247601333333 | 3.116930139270 | signal_rebalance |
| 11:30:32.776779 | Clear Pond | ZEC spot | -1.5046 | 1537.396506777881 | 1.619216748869 | signal_rebalance |

Cycle IDs are `forecast:` followed by:

- BTC: `faf3813607104f3a8c4e95eeeebadfaa`.
- ETH: `98476a78cc514f0b9fb7650fb265670a`.
- HYPE: `dc645f7f8093486f97b5bdf37ed5843c`.
- ZEC: `aa459de88b3f4e478fa9aa4d2ba34d81`.

### Why each holding disappeared

**BTC: risk liquidation with a Research forecast explicitly excluded.** The
rejected BTC forecast had `qualified=false` and P(not-down)
`0.49559198243818575`; its model was `20260926T111514Z-2715bf62`. The decision
retained the intended hold target of $16,846.084613947598 in
`proposed_target_notional`, while the final risk target became zero. The fill
has no acting forecast/model ID and reason `risk_cap`.

Opening gross exposure was $50,900.48044344586, against a 60% pool cap of
$25,259.71798619151 and a 15% symbol cap of $6,314.9294965478775. Before BTC was
processed, *other-symbol* gross was already $34,054.39582949826, above the entire
pool cap. The sequential formula therefore gave BTC zero pool capacity and sold
all executable BTC, leaving 0.0000088183 BTC dust. This was not an ordinary
Research-driven sell. It was an immediate risk adoption decision, with an
order-dependent all-or-nothing consequence.

**ETH: an eligible short opportunity was considered before risk capacity was
released.** P(not-down) was `0.41890971341624`, Qualified, from model
`20260926T111519Z-1f0a2d48`. The uncapped short target was $3,955.2832343035525.
After BTC was sold, the remaining gross $34,055.13808344586 still exceeded the
then pool cap $25,250.502946379263. The logged result was
`gross_capacity_exhausted`, with zero final target and no fill. Once HYPE and ZEC
were processed later in that same tick, capacity became available, but the
already-consumed ETH forecast was not normally retried. Thus the initial near
flat period was not proof that no Qualified opportunity existed.

**HYPE: neutral Qualified signal plus a historical-entry stop.** P(not-down)
was `0.4909708106088503`, Qualified, model `20260926T111525Z-2de25bb8`. This is
inside the exit band, so strategy targets became flat. Jeremy's long and Clear
Pond's spot holding were sold as `signal_rebalance`. Alex's inherited short was
already down **7.230621340558%** relative to its historical entry 85.8225 at the
seed mark 92.028, beyond the 3% stop. Its buy-to-close was labeled `stop_loss`.
Even changing the stop reference alone would not preserve the other HYPE
positions under immediate adoption of this neutral signal.

**ZEC: opposite Qualified signal, but the matching short was stopped too.**
P(not-down) was `0.4304026719758668`, Qualified, model
`20260926T111530Z-a40677ca`. The strategy's short target was
-$3,210.3199823886825, with logged policy reason `hold_with_hysteresis`.
Jeremy's long and Clear Pond's spot holding were sold because they opposed the
short direction. Alex could hold the short role, but its inherited entry
1346.6497 versus seed mark 1539.6 already represented a **14.328173095052%**
historical-entry loss. The 3% stop overrode the proposed short target to zero.

Alex's HYPE and ZEC stops created one-horizon cooldowns. With four 15-minute
bars, that is one hour. At the 11:45:23 decisions, the live stored checks show
deadlines **12:30:32.770495 UTC** for HYPE and **12:30:32.776259 UTC** for ZEC.
These use the in-process execution planning timestamps; rebuilding from saved
fill commit timestamps yields 12:30:32.771112 and 12:30:32.776779, respectively,
a sub-millisecond distinction. HYPE was then in the entry deadband, so its
cooldown was not the binding constraint. ZEC still had a Qualified short target
of -$2,958.1870839881626, explicitly blocked by `stop_cooldown`.

Precision rounding left five spot positions with total opening-mark exposure
$0.935143445859: Alex and Jeremy's passive HYPE dust, plus Clear Pond residuals
0.00113363 HYPE, 0.0000088183 BTC and 0.000055136 ZEC. No meaningful perpetual
position survived the first tick.

## Exact equity attribution

For signed fill quantity `q`, execution-time mark `m`, raw-book VWAP `v`, and
fill price `p`, the execution loss relative to the mark is `q * (p - m)`,
decomposed into `q * (v - m)` for book/mark difference and
`q * (p - v)` for explicit added slippage. The initial fills all used opening
marks, so there was no intervening mark-price movement in this first-cycle
calculation.

| Initial fill | Fee | Raw book versus opening mark cost | Extra 2 bp slippage | Total equity cost |
| --- | ---: | ---: | ---: | ---: |
| Clear Pond BTC | 11.789241241088 | 0.200130000000 | 3.369028446000 | 15.358399687087 |
| Alex HYPE | 3.313972116891 | 0.669899999999 | 1.472581980000 | 5.456454096891 |
| Jeremy HYPE | 2.484232054200 | 0.060000000000 | 1.104324000000 | 3.648556054201 |
| Clear Pond HYPE | 1.613427625925 | 0 | 0.461071536000 | 2.074499161925 |
| Alex ZEC | 4.331272331250 | 0.625000000001 | 1.924624999999 | 6.880897331250 |
| Jeremy ZEC | 3.116930139270 | 0.302999999999 | 1.385579400000 | 4.805509539270 |
| Clear Pond ZEC | 1.619216748869 | 0.595750000000 | 0.462725902000 | 2.677692650869 |
| **Total** | **28.268292257492** | **2.453779999999** | **10.179936264000** | **40.902008521492** |

The equality agrees with the committed equity loss to less than $0.00000000001.
Funding and transfers were zero. The book/mark component includes spread and
depth consumption; the saved snapshots do not warrant relabeling that component
as elapsed market movement.

Historical-cost realized P/L after the seven fills is -$736.856641264. That
number must not be mistaken for experiment loss: opening perpetual unrealized
P/L was already -$724.222925. The accounting identity is:

```text
experiment P/L = realized P/L + current unrealized P/L
               - opening unrealized P/L - fees + funding
              = -736.856641264 + 0 - (-724.222925) - 28.268292257492 + 0
              = -40.902008521492
```

Account-level first-cycle losses were Alex $12.337351428140, Jeremy
$8.454065593470, and Clear Pond $20.110591499877. Those amounts sum to the pool
loss; there is no unexplained cash adjustment in this reconstruction.

### Screenshot-time comparison

The ledger row at **11:41:08.744450 UTC / 04:41:08 PT** matches the Paper
screenshot:

- Equity $42,058.628380984235, displayed as **$42,058.63**.
- Experiment P/L -$40.901596001623, displayed as **-$40.90**.
- Fees $28.268292257492, displayed as **$28.27**.
- Gross exposure $0.935555965719, displayed as **$0.94**.

The change from the first completed tick to this observation is only
**+$0.000412519860** from marking the residual spot dust. Thus the visible flat
chart after the initial drop is economically consistent with liquidation.

The Actual screenshot shows **$42,102.15 at 04:41:32 PT**, 24 seconds later than
the Paper observation. The displayed difference is **$43.52**, while the user
described the starting gap as approximately $60. Those are separate statements.
Arithmetically, $42,102.15 is $2.620023014146 above the Paper opening baseline,
and the Paper observation is $40.901596001623 below it. Their sum explains the
$43.521619015769 difference before display rounding. The later Actual
observation is not an atomic counterfactual valuation of the seed. Its raw
account responses and marks are not retained in this evidence, so the $2.62
comparator component cannot be independently assigned solely to market movement
or source-read timing.

### Progress after the screenshots

At **11:45:23.345467 UTC**, a new Qualified ETH publication
`e54a4acf537d419882d2f0c0089c99d0`, model `20260926T113017Z-789f33d1`, had
P(not-down) **0.4572369730816276**. This passed the saved 46% short threshold;
Alex entered **-0.5475 ETH perpetual** at **2689.66196**, with fee
**$0.662665465395**. Its mark was 2690.1, raw-book bid 2690.2, and explicit added
slippage cost $0.2945769. The favorable raw book versus mark difference was
$0.05475, for net execution drag $0.2398269 before fees.

At the consistent **11:51:46.765475 UTC** cutoff:

| Attribution from opening | Dollars |
| --- | ---: |
| Opening equity | 42099.529976985854 |
| Total fees, eight fills | -28.930957722887 |
| Net raw book versus execution-time marks | -2.399029999999 |
| Explicit 2 bp added slippage | -10.474513164000 |
| Mark movement after executions | +0.165039995880 |
| Funding / net transfers | 0 |
| Equity | **42057.890516094856** |
| Experiment P/L | **-41.639460890998** |

The positive mark movement comprises +$0.16425 on Alex's ETH short as its mark
moved from 2690.1 to 2689.8, plus +$0.000789995880 on the residual spot dust.
Gross exposure was $1,473.601433441740. It would therefore be inaccurate to say
that the later live experiment still had only dust or no executed new entries.
That later activity does not cure the disputed opening semantics.

## Opening visibility and correction boundary

There are **zero `equity` rows at or before the seed timestamp**. The first
journaled equity point follows the BTC risk sale and is already down $15.36;
the opening exists in the seed/opening-snapshot metadata. First-cycle trading
started less than a second after that seed. The user had no useful opportunity
to inspect a visible $0-P/L mirror before strategy and risk adoption.

The runtime exports Parquet/performance after trading changes or after its
900-second export interval, while it commits mark observations every cycle.
The screenshot's 04:30:32 export time is consistent with the first-tick export
remaining unchanged during the quiet interval, even though the ledger advanced
to 04:41:08. At inspection, `performance.json` had advanced to
**11:45:23.373280 UTC**, after the new ETH fill. Export staleness must not be
used to dismiss the already-committed first-cycle liquidation. Panel-specific
ledger/export routing should be verified in the view/UI correction audit.

After this reconstruction, the user clarified the practical lifecycle: directly
fetch the current actual accounts, create the fresh 1:1 mirror, and then run
Paper trading with real simulated fees and current execution prices. The user
requested a temporary pause during the refresh and subsequently reaffirmed
that trading should start after the mirror is prepared. This is not an
indefinite pause or a pending abstract policy decision.

The old Paper worker was confirmed stopped at **11:56:22 UTC**, with its final
commit at approximately **11:55:28 UTC**, as reported by the coordinating
operational investigation. Maintenance is coordinated separately from this
read-only forensic inspection. The disputed run will be preserved before the
fresh mirror is prepared; the source paths above identify the original evidence
location and will need the archive mapping when that operation completes.

The corrected behavior being implemented is to persist and display a separate
opening observation with the freshly fetched signed quantities, source cash,
historical perpetual entries, $0 experiment P/L and zero seed fills/fees.
Inherited stops use a separately persisted **opening-mark risk reference**;
historical entry remains intact for cost-basis accounting and display. Future
account fetches record request start/end times and available exchange timestamps;
the absent original request timestamps are not fabricated or backfilled.

The operational sequence is prepare-only, verify the untouched opening, then
resume trading and inspect the first committed cycles. From that point,
Qualified neutral/opposite signals and exposure caps can still reduce inherited
holdings, and actual simulated executions incur the configured fees, book-depth
prices and slippage. The change does not grant inherited positions indefinite
exemptions from strategy decisions or risk controls. Preserving the visible
opening and removing pre-experiment losses from the fresh stop trigger are
distinct from promising that positions will stay unchanged after trading starts.

This forensic audit itself has performed no reset, stop-reference migration,
cooldown erasure, seed entry rewrite or cap exemption. Final fresh-seed and
first-cycle verification belongs in the operational follow-through record.

The saved 4 percentage-point entry band, Qualified-only eligibility, subsequent
execution costs, and source-signed inventory are not the accounting defect.
Any correction must retain their auditability and must be tested through first
committed cycles, not declared successful solely from a seed reconciliation.
