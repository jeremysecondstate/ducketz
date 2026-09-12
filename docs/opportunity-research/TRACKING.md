# Recommendation tracking v1

## Original calls and subsequent changes

The first published recommendation for a stable CIK/share-class identity
creates a call. Freeze its publication time, original dossier, valuation,
benchmarks, and six-/twelve-month windows. Watch and pass entries are preserved
as research but do not start hypothetical holdings. A later first upgrade
from watch to recommend starts tracking at that later publication.

Repeating a recommendation does not restart its clock. Keep thesis changes as
dated events. Status can be active, strengthened, weakened, withdrawn,
target_reached, or falsified. Withdrawal never erases or stops the fixed-window
performance measurement. A new independent thesis for a previously tracked
share class would require an explicit future policy extension; v1 preserves
one inception per identity.

## Entry and evaluation clock

Use the first XNYS regular-session opening strictly after actual publication.
A Sunday report normally enters Monday at 09:30 Eastern. Holidays, early
closes, and daylight-saving changes come from exchange-calendars. Do not
pretend Friday's reference close was an actionable weekend entry. The return
convention is hypothetical next-open entry, not a verified market fill.

Six and twelve months mean calendar-month anniversaries of the entry session.
If the anniversary is not a session, use the next session's official close.
Do not score an endpoint before its actual closing timestamp. Inception
tracking ends at the fixed twelve-month endpoint. Frozen mature results remain
in all future scorecards. Pending and missing-data counts are always visible.

Past-week return runs from the completed session at the prior weekly boundary
to the latest completed session, using adjusted close to adjusted close.
For a call first entered during that interval, it starts at its entry open.
After the twelve-month window completes, this field is no longer updated.

## Returns and benchmarks

FMP's `historical-price-eod/dividend-adjusted` endpoint supplies `adjOpen` and
`adjClose` for all companies and benchmarks. Require the declared adjustment
basis and a complete sequence of required sessions. Compute each return using
both endpoints from the same archived provider-response vintage:

`total_return = adjusted_end_close / adjusted_entry_open - 1`

Do not divide a newly adjusted close by an adjusted entry from an older
snapshot. Historical prices can be rescaled by subsequent dividends or splits.
This is a provider-adjusted total-return measure; retain the exact source
response so its conventions and revisions can be inspected. Do not silently
substitute raw prices or a different provider if adjusted history is missing.

The broad benchmark is SPY. Freeze a separate appropriate sector/style ETF
with rationale for every call, including differences in size, risk, and sector
exposure. Use identical entry and exit sessions. Excess return is the stock
total return minus the benchmark total return, expressed in percentage points.
It is not a factor-adjusted alpha estimate.

Maximum drawdown is the worst decline from a running peak of daily closing
total-return wealth, seeded at entry wealth 1. It excludes intraday drawdowns.
No transaction costs, slippage, taxes, portfolio weights, or actual trading are
assumed. Report calls individually and summarize with explicit sample counts;
overlapping calls, repeated sectors, and shared market dates are correlated.
The average recommendation return is not a simulated portfolio return.

## Missing data, corporate actions, and revisions

Never use a later observation as a substitute for a missing entry, a stale
last quote as a matured exit, or an invented zero for a delisting. Missing
sessions, mergers, symbol changes, bankruptcies, and unavailable histories
remain visible as missing outcomes. Reconcile the actual corporate action and
source history before resolving them; v1 does not automatically model merger
consideration or delisting proceeds. A missing losing company must remain in
the denominator of coverage counts. Successful and failed adjustments must be
explained in the weekly report.

Once a mature 6m/12m result is successfully evaluated, carry its full evidence
record forward unchanged. Revisions in provider data do not silently rewrite
the score. Discovered errors require a conspicuous correction discussion in a
later edition; v1 does not provide destructive history editing. Hashes detect
accidental corruption but are not signatures or an external proof of timing.

## Business outcomes and later modeling

Every week, assess the original catalyst success tests, financing expectations,
and forecast assumptions against actual filings and company developments.
Describe price changes separately from whether the thesis was right. Preserve
forecast figures and realized evidence in the original dossier and updates.
V1 automatically computes market-return outcomes; business-forecast accuracy is
reviewed by the analyst and is not yet a standardized numerical training label.

Potential later supervised targets are 6m/12m excess total return, drawdown,
catalyst outcomes, and revenue/margin/FCF forecast errors. Training requires a
broad point-in-time historical panel, original as-published facts, delisted
companies, chronological testing, and controls for overlapping label windows.
Do not train a neural network from a few weeks of immature recommendations.

Provider documentation:
- https://site.financialmodelingprep.com/developer/docs/stable/historical-price-eod-dividend-adjusted
- https://www.sec.gov/search-filings/edgar-application-programming-interfaces
