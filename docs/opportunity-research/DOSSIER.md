# Analyst dossier format

Author JSON in the prepared week's `research.json`. Numbers are JSON numbers,
not formatted strings. Timestamps include UTC offsets; dates use YYYY-MM-DD.
All source IDs are local to their candidate/update. Never include credentials.
Preserve factual source timestamps; do not backdate the dossier or publication.

## Edition object

| Field | Type / meaning |
|---|---|
| week | Sunday edition date |
| research_cutoff | Actual UTC cutoff after all source access and snapshot fetching |
| summary | Weekly findings, changes, strongest opportunities and uncertainty |
| screening | Objects with symbol, track (established/emerging), disposition (investigate/watch/pass), reason |
| candidates | Full investigation objects below |
| updates | One update for every unresolved call ID from brief.json |
| limitations | Array of specific coverage/data limitations; empty if none |
| no_new_recommendations_reason | Required explanation when no first-time recommendations qualify |

Aim for at least twelve screened names and four investigations, balanced by
track. Two investigations are acceptable. Less than twelve names/two memos or
coverage of only one track requires an explicit limitation. Do not add filler
to meet the count. A data outage may justify an edition with no new memos,
provided prior-call updates and all missing outcomes are honestly reported.

## Candidate object

Identity and observation fields:

- `symbol`: current uppercase ticker.
- `security_id`: `CIK` + ten-digit CIK + `:` + stable share class, for example
  `CIK0000000001:COMMON`. Verify the actual identifier from the filing/profile.
- `company_name`, `track`: `established` or `emerging`, `currency`: `USD`.
- `decision`: `recommend`, `watch`, or `pass`.
- `reference_price`, `reference_session`: latest completed daily close, tied
  to `daily_prices` in the archived FMP company snapshot.
- `median_daily_dollar_volume_20d`: median(close * volume) over the latest
  twenty observed sessions. The validator independently recomputes this.
- `cash_runway_months`: conservative numeric months; mandatory for emerging
  businesses. Omit for established businesses if not meaningful.
- `sector_benchmark`: an appropriate, available ETF ticker. Freeze on the
  original call; SPY is also tracked automatically for every call.

Each following field is an explanatory string:

- `business`: business model, customers, geography and material segments.
- `product_evidence`: customer value, actual deployments and economics.
- `mispricing`: the specific supported disagreement with priced-in assumptions.
- `reverse_valuation`: growth, margins, adoption and financing required to
  justify today's price, and why those requirements look plausible or wrong.
- `financing`: cash, burn, debt deadlines, capital needs, dilution, survival.
- `accounting_adjustments`: R&D, capex, stock compensation, leases, warrants,
  other senior claims, share-count units and reconciliations.
- `countercase`: the strongest substantive opposing explanation.
- `invalidation`: observable conditions that would break the thesis.
- `benchmark_rationale`: why the sector/style benchmark is an appropriate
  comparator and any meaningful differences from the company.

`sources` is an array of objects:

```json
{
  "id": "filing",
  "title": "Latest quarterly financial filing",
  "url": "https://www.sec.gov/Archives/actual-document-path",
  "kind": "filing",
  "published_at": "2026-09-10T20:15:00Z",
  "accessed_at": "2026-09-13T16:15:00Z",
  "supports": "Exact claim and filing section or page supporting it."
}
```

Use actual URLs and times, not the example. Allowed kinds: `filing`, `company`,
`independent`, `market_data`, `social`. `published_at` may be null when unknown;
`accessed_at` is always required. At least one source in each of the first four
kinds is required for a recommendation. For FMP evidence, cite the relevant
public company/data documentation URL and preserve the exact numerical response
through the snapshot reference; do not expose an authenticated API URL.

`scores` has all five keys from SCORECARD.md. Each value has numeric `rating`
from 0 to 5, explanatory `rationale`, and nonempty `source_ids`:

```json
"valuation": {"rating": 3, "rationale": "Explain the valuation evidence and uncertainty.", "source_ids": ["filing"]}
```

`gates` has all six keys: `identity_verified`, `financials_reconciled`,
`liquidity_sufficient`, `financed_through_catalyst`, `primary_corroboration`,
`countercase_complete`. Each has boolean `passed`, explanatory `rationale`,
and nonempty `source_ids`. A failed gate forces watch/pass. A source reference
is auditable evidence; the validator cannot establish that a claim is true.

`catalysts` is an array of objects with `due_date`, `milestone`, `success_test`,
`failure_consequence`, and nonempty `source_ids`. Use a conservative end date
for an uncertain window and explain that window in the milestone. A company
promise and an independently verified event are different evidence.

## Numerical valuation inputs

`valuation` has `bear`, `base`, `bull` objects. Each uses one of these formats.
Inputs here are illustrative schema examples, not a real company valuation.
Use consistent USD millions and million diluted shares.

```json
{
  "method": "dcf",
  "fcff": [100, 110, 120, 130, 140],
  "discount_rate": 0.12,
  "terminal_growth": 0.025,
  "net_debt": 200,
  "diluted_shares": 100,
  "assumptions": "Explain revenue, margins, taxes, reinvestment, terminal economics, debt, share count and evidence."
}
```

```json
{
  "method": "comparables",
  "metric": "revenue",
  "forward_metric": 1000,
  "multiple": 3,
  "net_debt": 200,
  "diluted_shares": 100,
  "assumptions": "Name peers, justify the current forward multiple, and explain growth, margin, risk and capital-intensity adjustments."
}
```

The numerical calculator supplies present per-share values. Their order must
be bear <= base <= bull, with positive base value. Comparables accept revenue,
ebit, ebitda, or fcff as enterprise metrics. DCF uses FCFF/cost of capital and
includes a sustainable terminal value. To capture financing or survival risk,
use explicit conservative cases and explain the treatment; never imply the
three scenarios are calibrated probability estimates.

`price_targets` has `6m` and `12m` objects, each with numeric `bear`, `base`,
`bull` share prices and a `rationale` for the catalyst-driven path. These are
future price scenarios, separately justified from present-value estimates.
For watch/pass cases where a defensible estimate is unavailable, set
`valuation` and/or `price_targets` to null and explain why in the memo.

## Prior-call update

Use the exact immutable `call_id` from `brief.json`. Each update has:

- `call_id`.
- `status`: active, strengthened, weakened, withdrawn, target_reached, falsified.
- `thesis_update`: explain what changed or why the original thesis still holds.
  Distinguish stock-price movements from operating evidence. Cite actual metrics
  versus originally forecast figures where available.
- `catalyst_update`: assess original success tests, completed/missed milestones,
  shifted dates, financing and upcoming evidence.
- `sources`: the source format above, at least one dated source/check even for
  unchanged conclusions. Explain unavailable data rather than invent evidence.

Numerical performance is computed from snapshots, not authored into this
object. If a company is investigated again, it may also appear in candidates;
its original benchmark, identity, and inception remain unchanged.
