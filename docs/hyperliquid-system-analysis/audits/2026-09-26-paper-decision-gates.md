# Qualified forecasts and Paper execution gates

Investigation of the user's **03:45 PT** decision screenshot on 2026-09-26.

**Excluded Paper sample, deletion completed:** the user subsequently directed
that the run seeded at **2026-09-26T10:35:59.867633343Z** be deleted without an
archive or analytical use. Automatic approval review initially blocked recursive
deletion; the user then completed it. The temporary cleanup directory
`C:/DATASTORE/hyperliquid/_pending_deletion/20260926T103559Z-excluded-paper` was
verified absent at **2026-09-26 11:36:32 UTC**. It was not an analytical archive.
The sample remains permanently excluded from analysis and recovery. Its
screenshot-derived observations, balances, fills, performance
and deployment evidence are excluded here. This is a user-chosen evaluation
exclusion, not proof that every Hold was a defect. This audit retains the code
explanation, software verification and clearly separate earlier-archive analysis.

## What Qualified and Hold mean in the code

Qualified describes the published model's eligibility; it does not bypass entry
probability, account direction, cooldown, sizing or execution gates. A shared
forecast can yield three account rows with different actions. Alex permits short
perpetual exposure, Jeremy long perpetual exposure, and Clear Pond long spot
exposure. Research forecasts are excluded from signal allocation.

Inside the entry deadband an otherwise eligible forecast can correctly produce
a Hold. Active cooldown can block an entry, and a requested dust exit can round
below quantity precision or the simulated fill minimum. A decision is not an
order, and a zero-quantity simulated attempt is not a fill.

The generic top-level `signal_rebalance` reason did not explain those account
checks. The diagnostic path also omitted flat accounts from cooldown attribution
even though the runtime already suppressed their new targets during cooldown.

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

The original same-ledger phase is superseded by the user's exclusion/reset request.
The replacement experiment must begin from its own fresh mirror and retain its
own policy/forecast/model IDs and timestamps. Within that new sample, identify
direction entries in the additional 4–5 percentage-point band separately from
entries that also meet the prior threshold, retained-position
rebalances, stop/cap exits and initialization costs. Existing matching positions
use exit hysteresis, so probability alone cannot identify an additional entry.
Costs and realized outcomes belong to actual executed quantities, not desired
targets. Any comparison against the separate earlier archive is descriptive,
not a simultaneous controlled estimate of performance. The excluded sample
must not supply a comparison period.

No Powder runtime is activated. Its future activation reads the shared policy
configuration and will therefore see this entry setting unless changed later.

## Independent earlier-archive analysis

The separate earlier archive
`C:/DATASTORE/hyperliquid/_paper_archives/20260926T102933Z-qualified-15m-v1`
was not targeted by the deletion request. Its forward journals contained 128
matured forecasts (32 per market), 65 Qualified at publication. Simple flat-entry
eligibility at 4 versus 5 percentage points rose from 38 to 44: five additional
ETH forecasts and one ZEC forecast. These are eligible forecasts, not six
promised fills; account holdings, cooldowns, sizing and direction still matter.

The five additional ETH observations averaged +0.84 bps signed one-hour mark
return; the one ZEC observation +54.30 bps. A 13 bps perpetual round-trip
fee/slippage proxy makes those -12.16 and +41.30 bps respectively. This small,
selected, overlapping sample omits spread, funding, actual execution, size and
the real strategy's variable holding periods. It supports an exploratory
forward phase, not a conclusion that quality or profitability improves.

These observations come from that earlier archive, not the excluded sample.

## Software verification

**412 relevant tests passed:** 75 runtime, 32 workspace, and 305 across policy,
market simulation, ledger, view adapter and Powder runtime compatibility. Tests
cover the added entry band, retained neutral region, saved versus current UI
thresholds, actual skip reasons, cooldown expiry and unchanged execution/retry
behavior. Independent source review found no execution change in the diagnostic
repair. Those tests establish implementation behavior, not a return estimate.
The excluded run's backups and phase artifacts were included in the user-completed
deletion. Its permanent exclusion from evaluation and recovery remains in force.
Replacement-run activation and watch evidence belong to the fresh experiment's
separate record.
