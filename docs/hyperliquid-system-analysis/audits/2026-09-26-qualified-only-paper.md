# Qualified-only Paper experiment

User-requested fresh mirror experiment, **2026-09-26**. Setup and verification
are recorded below. This experiment concerns simulated Paper; Powder remains
inactive and no real order, cancellation or transfer is part of the reset.

## Archived control run

The previous Research + Qualified run was stopped gracefully. Its final
committed observation was **07:36:19.336411 UTC**:

| Measure | Final value |
| --- | ---: |
| Opening equity | $42,078.277612908096 |
| Closing equity | $41,854.809725725194 |
| P/L since opening | -$223.467887182904 |
| Simulated fees | $146.416794546919 |
| Estimated funding | +$0.129371805714 |
| Committed cycles / fills / virtual transfers | 744 / 146 / 12 |

Complete archive:
`C:/DATASTORE/hyperliquid/_paper_archives/20260926T073643Z-research-and-qualified`.
It contains the entire stopped `_paper` directory, opening snapshot, versioned
policies, funding cursor, exports and runtime logs; configuration/source copies;
`final-summary.json`; and a SHA-256 file manifest. The archived database passed
SQLite `quick_check` after the directory move. No historical loss or fee record
was rewritten. The old seed remains in the archive rather than the new run.

## Experiment contract

- `configs/hyperliquid-paper.json`: `seed_mode=mirror`,
  `require_qualified_forecasts=true`.
- Paper recipe: **direction-volatility-v2-qualified-hold**. Only a valid fresh
  Qualified forecast can drive signal allocation. A claimed Qualified forecast
  must identify an `active` role and an `eligible=true` model record.
- Research, missing, stale or invalid forecasts do not drive a signal trade.
  Existing targets are held pending an eligible signal. Risk reductions for
  stop loss, cooldown and exposure limits remain active and use fresh executable
  books. This also removes signal-expiry-only close/reopen behavior from this
  experiment without extending forecast freshness or using stale probabilities.
- Inherited holdings can remain present in a market without a Qualified model;
  this is retaining the mirrored inventory, not opening it from a Research
  signal. Independent limits/stops can still reduce it immediately after opening.
- Rejected forecasts retain audit provenance under decision policy details,
  but do not become the recorded model source of a hold or risk reduction.
  The revised UI shows **No forecast** / **Risk exit** and a fill **Reason**.
- No fitting, calibration, promotion threshold, fee rate or allocation threshold
  was changed. Data/models continue running and may publish Research forecasts;
  Paper excludes them. The legacy mixed-model recipe retains its former fallback
  behavior when `require_qualified_forecasts=false`.
- The opening mirror captures current inventory and collateral through public
  account reads. It is not atomic across accounts, does not import open orders
  or reproduce the exchange's margin/liquidation engine, and assigns inherited
  spot cost basis at the opening mark. It does not stay synchronized afterwards.
- Powder references the same Paper configuration, so its future qualification
  gate also becomes qualified-only. Its existing missing-signal zero-target
  behavior is unchanged; it does not inherit this new Paper hold behavior.

This is a new experimental recipe, not an isolated statistical estimate of the
effect of qualification: both qualification inclusion and the handling of
missing signals differ from the archived run. A Qualified badge alone does
not establish profitability after costs.

## Operations and verification

Operations Watch was temporarily paused and a maintenance record was written
before the deliberate Paper stop/archive. Data and models were preserved.
The new seed, active policy, process identity, committed observations and
qualified-only signal provenance must be verified before clearing maintenance
and restoring the watch. Scheduled recovery must use the new baseline and must
never restore/reseed from the archived control run merely to match old memory.

The new mirror opened at **2026-09-26T07:42:57.850335836Z**:

| Account | Opening equity |
| --- | ---: |
| Alex | $5,918.11491663872 |
| Jeremy | $6,123.06770195568 |
| Clear Pond | $30,030.5081411710 |
| Pool | **$42,071.6907597654** |

The nine inherited positions and `seed_mode=mirror` were verified. Saved policy
`60b7f962240a0857` has the qualified-only flag and v2 recipe. At opening, ETH/ZEC
had qualified forecasts and BTC/HYPE had Research forecasts, which were excluded.

Initial Paper worker **66424** was launched independently through WMI and hidden
shell **44760**, outside the Codex process tree. Two observations at 07:43:28
and 07:44:30 UTC showed advancing heartbeats and committed cycles **5 → 7**,
unchanged seed and no errors. The first seven fills comprised two qualified
signal rebalances and five independent stop/cap reductions; no Research-driven
growth or virtual transfers occurred. Reconstructed inherited quantities
confirmed that every unattributed fill reduced exposure without crossing zero.
The first cycle's fees were **$26.90848417**, and its P/L **-$38.63116630**:
initial risk/signal adjustments have costs even though the opening baseline is new.

A subsequent review caught missing forecast IDs bypassing per-coin risk checks.
The reader now rejects missing/blank/non-string IDs before allocation, allowing
the Paper invalid-signal risk path to run. A targeted regression confirmed that
an otherwise necessary stop still executes. Paper was gracefully restarted to
load this fix, retaining the same seed, policy and ledger. Initial source
snapshots and the later reader update are both retained under
`_paper/experiment-sources`, with hashes in `_paper/experiment.json`.

Verification: **374 selected tests passed** across runtime, forecast reading,
seed, policy, ledger, read-only view, Powder runtime and workspace UI. After the
forecast-ID fix, **77 reader/runtime tests passed**, including five added cases;
the other 302 selected cases had already passed. This covers **379 distinct
test cases**. These use fake exchange observations and temporary ledgers;
the production mirror itself used public read-only account information.

The running Duckets UI continues reading the new ledger automatically. Its new
Reason column and No forecast/Risk exit display require reopening Duckets to
load the updated UI source; restarting the independent Paper worker is not
required for that display change.

Final verification at **07:46:10 UTC** identified Paper process **73216**, started
07:45:06 UTC through the independent launch chain. Committed marks at
**07:45:09 → 07:45:40 UTC** both followed that restart. The same new seed and
policy remained intact, with 17 committed cycles, two qualified signal fills,
five independent risk reductions and no disallowed signal fill or virtual
transfer. All 17 UI sources were fresh in the preceding read, with no runtime
errors and Powder still unactivated. Source hashes matched the saved launch
snapshots plus the recorded reader update.

The maintenance record was marked completed, the watch memory was updated to
the new baseline, and **Hyperliquid Operations Watch** was restored to **ACTIVE**
with its existing 30-minute GPT-6 Luna / Extra High schedule and v2 contract.
