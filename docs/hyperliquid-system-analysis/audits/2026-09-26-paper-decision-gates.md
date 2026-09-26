# Qualified forecasts and Paper execution gates

Investigation of the user's **03:45 PT** decision screenshot on 2026-09-26.
The active Paper ledger was seeded at 10:35:59.867633343 UTC with
$42,091.465038572365. This change preserves that experiment and its history.

## What the screenshot actually recorded

Read-only SQLite inspection of the 10:45:32 UTC forecast cycles found:

| Forecast | Account/action | Actual check |
| --- | --- | --- |
| ETH, P(not-down) 45.458679% | Flat accounts Hold | New short requires at most 45%; saved policy reason `entry_deadband` |
| HYPE, 49.127839% | Alex/Jeremy Hold | Signal inside the new-entry band |
| HYPE, 49.127839% | Clear Pond Skip, $0.104317 current | Attempted dust exit rounds below quantity precision |
| ZEC, 39.996146% | Alex Hold | A 10:36:00.505351 stop fill imposed cooldown until 11:36:00.505351 UTC (04:36 PT) |
| ZEC, 39.996146% | Jeremy Hold | Short signal belongs to Alex; Jeremy is long-only and already flat |
| ZEC, 39.996146% | Clear Pond Skip, $0.084650 current | Attempted dust exit rounds below quantity precision |
| BTC, Research excluded | Three Holds | Qualified-only signal gate, correctly recorded |

The screenshot's Qualified status was correct. The generic top-level
`signal_rebalance` reason was not a useful account-level explanation. A further
diagnostic bug omitted flat accounts from cooldown attribution even though the
runtime correctly suppressed their new targets during cooldown.

No missing successful execution was found: a decision is not an order, and a
zero-quantity simulated attempt is not a fill. Every shared forecast can yield
three account rows with different allowed directions and risk constraints.

## Diagnostic repair

The runtime records specific Hold/Skip explanations and `decision_checks` with
actual thresholds, proposed target, final delta, account role/capacity, order
minimum and cooldown expiry/remaining time. Existing risk/fill reasons remain
the source of stop reconstruction. Flat cooldown attribution does not change
the held-position risk retry gate, forecast deduplication, targets or orders.

The UI displays the saved checks and actual nonfill reason. Older records use
only saved nested policy/execution facts; their original journals are not
rewritten and missing old cooldown details are not invented. The presentation
requires reopening the UI to load the new Python code.

## Broader entry phase for forward evaluation

The user clarified the objective: Paper should begin with broader participation
to collect executed outcomes, then tighten rules from evaluation evidence.
Changed the checked-in `entry_band` from 0.05 to **0.04**: new longs require
P(not-down) at least **54%**, and new shorts at most **46%**. This is a deliberate
forward sampling change, not a promotion based on demonstrated profit gains.

Exit hysteresis remains 52%/48%, normal adjustment minimum max($25, 10% of
target), separate simulated fill minimum $10, and post-stop cooldown one hour.
Qualified-only models, 15-minute training cadence, costs and account roles are
preserved. Lowering entry thresholds does not activate Research forecasts or
make a zero-quantity attempt a fill. The dataclass legacy default stays 0.05;
the deployed JSON explicitly selects 0.04.

The same opening seed, cash, positions and journals continue. A new persisted
policy ID identifies the phase; decisions/fills keep their policy/forecast/model
IDs and timestamps. Compare new direction entries in the additional 4–5
percentage-point band separately from old-rule entries, retained-position
rebalances, stop/cap exits and initialization costs. Existing matching positions
use exit hysteresis, so probability alone cannot identify an additional entry.
Costs and realized outcomes belong to actual executed quantities, not desired
targets. A before/after phase comparison is descriptive, not a simultaneous
controlled estimate of performance.

No Powder runtime is activated. Its future activation reads the shared policy
configuration and will therefore see this entry setting unless changed later.

## Evaluation and deployment evidence

Current fresh model history had eight forecasts, six Qualified, and no matured
one-hour outcomes at inspection. Archived forward journals contained 128 matured
forecasts (32 per market), 65 Qualified at publication. Simple flat-entry
eligibility at 4 versus 5 percentage points rose from 38 to 44: five additional
ETH forecasts and one ZEC forecast. These are eligible forecasts, not six
promised fills; account holdings, cooldowns, sizing and direction still matter.

The five additional ETH observations averaged +0.84 bps signed one-hour mark
return; the one ZEC observation +54.30 bps. A 13 bps perpetual round-trip
fee/slippage proxy makes those -12.16 and +41.30 bps respectively. This small,
selected, overlapping sample omits spread, funding, actual execution, size and
the real strategy's variable holding periods. It supports an exploratory
forward phase, not a conclusion that quality or profitability improves.

The original gate already executed an Alex short at 11:00:24 UTC when ETH's
down probability reached 56.5778%: 1.1022 ETH, $2,960.91 notional, $1.33 fee.
The execution path was working before this change. At the screenshot's earlier
45.46% P(not-down), the broader gate would have permitted that direction sooner,
subject to the other checks.

## Activation and preserved evidence

Gracefully stopped Paper PID 70028, verified it exited and saved a SQLite
backup, old policy/experiment/status/funding records and journal-prefix hashes.
The phase evidence lives at:
`C:/DATASTORE/hyperliquid/_experiments/20260926T103559Z-qualified-refresh/policy-phases/20260926T110630Z-entry-4pp`.

Resumed the same ledger as Paper PID 45828; first running observation was
11:06:46 UTC. Active policy is **993e26589020a5a0**, replacing
**60b7f962240a0857**. The only policy-value change is entry_band 0.05 to 0.04.
All 72 prior cycles, 46 decisions, eight fills, 288 equity rows and empty
funding/transfer journals matched their saved prefix hashes. The complete
opening snapshot was identical. New forecast IDs use the new gate; historical
decisions and already processed forecasts are not replayed to manufacture fills.

**412 relevant tests passed:** 75 runtime, 32 workspace, and 305 across policy,
market simulation, ledger, view adapter and Powder runtime compatibility. Tests
cover the added entry band, retained neutral region, saved versus current UI
thresholds, actual skip reasons, cooldown expiry and unchanged execution/retry
behavior. Independent source review found no execution change in the diagnostic
repair. The scheduled watch follows operating contract v4 and the new policy
while preserving the same original seed.

Verification at 11:08:37 UTC found all 17 sources fresh with no errors, warnings,
competing owners or Powder process. Committed observations advanced from
11:07:17.810688 to 11:08:21.643888 UTC, cycles 74 to 76. Paper's venv launcher
57800 and hidden cmd parent 63780 were outside Windows jobs, with a WMI ancestor.
The original stop-fill timestamps persisted, preserving the approximately
11:36 UTC cooldown deadlines. New diagnostic decisions had not yet appeared
because the existing forecast IDs remained deduplicated; the next publication
normally follows the next 15-minute candle. Runtime tests cover their payloads.
Evidence: `C:/DATASTORE/hyperliquid/_operations/entry-4pp-verification.json`.
