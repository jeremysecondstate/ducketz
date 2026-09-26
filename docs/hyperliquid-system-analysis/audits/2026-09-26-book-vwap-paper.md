# Paper mirror with book-VWAP execution — September 26, 2026

The user requested another clean mirror and then narrowed the pricing correction
to removing added slippage while retaining separate taker fees for perps and
spot. Paper was stopped during preparation. Data and models continued; Powder
remained inactive. No real account was mutated.

## Pricing and verification

`configs/hyperliquid-paper.json`, `PaperConfig` and `simulate_fill` now default
to zero extra slippage. The simulation consumes fetched executable order-book
depth and uses its quantity-weighted average price directly. Perp fee rate is
0.00045; spot fee rate is 0.0007. Fees apply to actual executed notional, including
partial fills. These are configured simulation rates, not a claim that each
account's fee tier was fetched. See the exchange's [fee documentation](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees).

Historical explicit nonzero-slippage scenarios remain supported and their
records are unchanged. The spot planning reserve is unspent cash, not a price
adjustment or charged fee. Qualified-only signals, entry/exit bands, caps, sizes,
cooldowns and the persisted opening-mark stop reference remain unchanged.

311 scoped tests passed (278 market/policy/runtime plus 33 ledger), covering
multi-level and partial-depth buys/sells, separate fee rates, cash and signed
holdings, realized P/L and equity. A read-only independent replay also verified
all 14 committed cycles through 12:39:36.397423 UTC. Every execution had zero
extra slippage, price equal to raw book VWAP, and the applicable fee rate.

## Opening and first trading tick

Experiment **20260926T123436Z-book-vwap**, policy **2c8197e2cbe140d6**, opened at
**2026-09-26T12:34:36.903077126Z** (05:34:36 Pacific).

| Account | Opening equity | Difference from source valuation |
| --- | ---: | ---: |
| Alex | 5812.349172182411 | -0.006585 |
| Jeremy | 6200.32835489704 | +0.004615 |
| Clear Pond | 30075.566092656707 | 0 |
| Pooled | 42088.24361973616 | -0.001970 |

Public holdings were mirrored with historical perpetual cost basis and a fresh
opening-mark stop reference. All nine inherited positions were retained before
trading. One opening cycle and four equity rows had zero decisions, fills,
transfers, funding, fees and experiment P/L. Request windows and available
exchange timestamps are preserved. Source account reads and quotes are not an
atomic exchange snapshot. Perp source UPNL and reconstructed mark-to-entry UPNL
can differ at subcent precision. The Actual UI also rounds values differently;
this audit does not claim exact cent-for-cent equality between live tabs.
No artificial cash reconciliation was applied.

Continuous Paper resumed after opening verification. Its first complete tick,
**12:35:21.942592 UTC**, had seven fills and pooled equity **42058.61151477942**:
P/L **-29.632104956738**, fees **26.9140638455**, added slippage **0**. The remaining
net difference reflects executable book prices and mark movement since the
opening. Signals/risk may reduce inherited positions immediately under the
existing strategy. This first-tick result is distinct from the untouched opening.

Read-only replay verifies spot cash changes by signed fill notional plus fees;
perp cash changes by realized P/L minus fees. Signed holdings, historical basis,
stop reference and marked equity reconcile after every committed cycle. By
12:39:36 UTC there were still seven fills, with equity 42075.25665641538 and P/L
-12.986963320777 as market marks changed. No transfers or funding were recorded.

## Preserved evidence and operations

All paths below are under `C:/DATASTORE/hyperliquid`:

- Prior run: `_paper_archives/20260926T120059Z-fresh-mirror-opening-replaced`.
- Preservation manifest: `_operations/paper-remirror-zero-slippage-preservation.json`.
- Untouched opening: `_operations/paper-remirror-zero-slippage-opening-verification.json`.
- First tick: `_operations/paper-remirror-zero-slippage-first-cycle-verification.json`.
- Later replay: `_operations/paper-remirror-zero-slippage-trading-accounting-verification.json`.
- Read-only replay script: `_operations/paper-remirror-zero-slippage-verify.py`.
- Health: `_operations/paper-remirror-zero-slippage-health-verification.json`.

The disputed 11:30:32 archive and earlier independent evidence are preserved.
The different, permanently excluded 10:35:59 sample remains excluded and deleted;
it was not restored or evaluated.

At **12:39:48.966083 UTC**, Paper actual PID **75292**, venv launcher **32084** and
hidden cmd **45380** matched the expected module/config and a fresh advancing
ledger. Data PID **57004** and models PID **56520** were unchanged. All 17 source
states were fresh, warnings and stop requests were absent, and Powder was
disconnected with zero pending intents. Operations Watch v7 follows this seed
and policy; recovery must preserve the existing ledger and opening. The only
config hash change is Paper (`05d72a159f939e3e557ff88409763305b73657e1faa07356b911c78e03e3a005`).

## Overlapping watch report resolved

The 12:40 UTC watch began with v6 and read the old maintenance marker while
the owner was finishing v7 documentation. Its health projection at 12:41:00 UTC
was healthy. The runbook changed at 12:41:17, app prompt at 12:41:47, and
maintenance was completed at 12:43:02.298343 UTC. The owner's accepted-memory
entry used an earlier health-check timestamp and claimed completion too soon.
The watch subsequently saved its earlier mismatch as an open incident, even
though completion had already been published by the time its final report ran.

A fresh read at 12:45:52 UTC confirmed completed maintenance, v7 runbook/prompt,
unchanged current seed, all 17 sources fresh and healthy data/models/Paper.
The ledger advanced to 12:45:26 UTC; Powder stayed inactive. The stale incident
is resolved in watch memory with its original evidence preserved. No worker was
stopped, restarted or reseeded to resolve this reporting issue. The runbook and
prompt now require one final reread before reporting a maintenance/version
mismatch, and require the owner to publish the completion marker before accepted
memory. A watch that observed maintenance remains observational for that pass.
