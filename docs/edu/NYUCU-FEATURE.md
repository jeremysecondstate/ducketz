These papers are **highly relevant** to what you are building. Together, they support a much better idea than simply blending a few accounting ratios into the existing technical score:

> Build a separate, point-in-time **fundamental earnings-direction signal**, then combine it with market-regime technicals only at the final signal layer.

## What the two papers are really saying

### 1. `CU-ML-FUNDVAL`: detailed line items contain hidden earnings information

The first paper predicts future earnings using 28 major financial-statement line items plus their first-order changes—56 features total. It finds that nonlinear models generally forecast earnings more accurately than traditional accounting models and the naïve random-walk benchmark.  

The most useful finding for Duckets is not necessarily the neural network. It is the feature discovery:

* Current earnings
* Operating cash flow
* Income-tax expense
* Change in income-tax expense
* Common equity
* Changes in assets and receivables

The models also identified sensible interactions such as:

* Sales growth versus cost-of-goods growth
* COGS versus inventory
* PP&E versus depreciation
* Accounts payable versus sales
  
  

Most interestingly, the authors took only five of the strongest features—common equity, earnings, operating cash flow, tax expense, and change in tax expense—and constructed a simpler model that still outperformed the established forecasting models. 

That simplified result is almost tailor-made for our first implementation.

### 2. `NYU-ML-FINVAL`: predict direction, not an exact earnings number

The NYU paper asks a slightly easier and more actionable question:

> What is the probability that next year’s earnings will increase?

Its models generated an out-of-sample AUC of roughly 67.5%–68.7%, compared with 50% for random guessing. The resulting historical hedge portfolios also produced economically meaningful size-adjusted return spreads. 

This is important because the authors separately tried predicting the exact earnings level and the exact amount of the earnings change. Those versions performed poorly, sometimes even losing to a random-walk forecast. Predicting the **direction** worked materially better. 

That suggests our output should not initially be:

```text
Expected NVDA EPS next year = $X.XX
```

It should be:

```text
Probability of improving forward fundamentals = 73%
Fundamental direction score = 73 / 100
Fundamental confidence = 81 / 100
```

## My recommended first indicator

I would call it:

# Duckets Fundamental Direction Composite

A transparent 0–100 score answering:

> Based only on information available as of the latest filing, how strongly are NVDA’s operating fundamentals pointing toward improvement or deterioration?

### Suggested components

| Component                       | Weight | Examples                                                                               |
| ------------------------------- | ------:| -------------------------------------------------------------------------------------- |
| Earnings momentum               | 25%    | Revenue growth, operating-income growth, net-income growth, margin direction           |
| Cash conversion                 | 25%    | CFO growth, free-cash-flow margin, CFO relative to net income                          |
| Accrual quality                 | 20%    | Net income minus CFO, receivables versus sales, inventory versus COGS                  |
| Balance-sheet resilience        | 15%    | Cash versus debt, current assets/liabilities, debt growth versus cash-flow growth      |
| Tax and earnings-quality signal | 10%    | Tax-expense trend, effective-tax-rate stability, tax expense relative to pretax income |
| Investment and dilution         | 5%     | R&D intensity, capital expenditures, stock-based compensation, share-count growth      |

Every component should remain visible, just like the present market-regime calculation exposes its trend, momentum, range, and volume components rather than storing only a final number. The current market-regime framework already follows that transparent pattern.

### Especially valuable interaction features

These are where the papers become more creative than ordinary ratio analysis:

```text
receivables_growth - revenue_growth
inventory_growth - cost_of_revenue_growth
operating_income_growth - revenue_growth
cfo_growth - net_income_growth
debt_growth - cfo_growth
ppe_growth - depreciation_growth
share_count_growth - net_income_growth
```

Examples:

* Revenue growing 30% looks good, but less so if receivables grow 70%.
* Earnings growing 25% looks good, but less so if operating cash flow falls.
* Inventory growth may be constructive when matched by demand, but dangerous when it greatly outpaces cost of revenue.
* Net income growth may not benefit shareholders if diluted shares grow nearly as fast.

Those mismatches are exactly the sort of nonlinear relationships the papers found valuable.

## The key architectural rule

Fundamental data should **not** pretend to be a one-minute or five-minute signal.

A quarterly filing-derived score should change only when new public information becomes available. Between filings, it should remain constant while its freshness slowly declines.

I would store the primary result as an event-time series:

```text
stocks/NVDA/fundamentals/fundamental-direction/fmp/quarterly.parquet
```

Example columns:

```text
symbol
fiscal_period
period_end_date
filing_date
effective_from
fundamental_score
fundamental_confidence
earnings_momentum_score
cash_conversion_score
accrual_quality_score
balance_sheet_score
tax_quality_score
investment_dilution_score
data_age_days
source_file_count
calculation_version
```

Then, when generating timeframe-specific technical Parquets, we perform an as-of join:

```text
bar timestamp
    ↓
most recent fundamental score publicly available by that timestamp
```

The joined technical output can contain:

```text
technical_score
fundamental_score
fundamental_confidence
fundamental_age_days
combined_conviction_score
```

But I would preserve `technical_score` unchanged. The fundamental calculation should complement it, not alter its meaning.

## A sensible combined score

For an initial version:

```text
combined_conviction =
    70% market_regime_score
  + 30% fundamental_direction_score
```

The fundamental weight should be reduced automatically when:

* The latest filing is old.
* Required fields are missing.
* Cash flow and earnings strongly disagree.
* Only annual rather than quarterly history is available.
* A filing amendment or inconsistent source record exists.

For example:

```text
effective_fundamental_weight =
    30% × fundamental_confidence × freshness_factor
```

This means a strong but nine-month-old filing cannot overpower current market behavior.

## What must change in fetching first

The current corporate lane already retrieves the right dataset families:

* Key metrics
* Ratios
* Income statements
* Balance sheets
* Cash-flow statements
* Growth statements
* SEC filing information

Verify whether the current statement requests explicitly specify a `period` or `limit`.  If not, for this calculation maybe we should add separate, clearly named histories such as:

```text
income_statement_annual
income_statement_quarterly
balance_sheet_annual
balance_sheet_quarterly
cash_flow_annual
cash_flow_quarterly
```

The quarterly datasets are essential. A handful of annual observations for one company is nowhere near enough to train a reliable company-specific machine-learning model.

## Why we should not train “NVDA ML” yet

Both studies rely on large cross-sectional samples:

* The first uses over 100,000 firm-year observations and rolling historical training windows.  
* The second uses thousands of company filings and more than 13,000 candidate features derived from XBRL data. 

A model trained only on NVDA’s five or ten annual records would mostly memorize NVDA’s history. It might produce a precise-looking probability with almost no statistical foundation.

The right progression is:

1. **Transparent formula now:** build the fundamental composite from NVDA’s quarterly history.
2. **Cross-company normalization next:** fetch a universe of stocks and calculate percentiles by industry and period.
3. **Machine learning later:** train a temporally separated cross-sectional model across many firms.
4. **Residual signal eventually:** compare the ML forecast with simple benchmarks and use the unexplained component as the genuinely “new information” signal, similar to the first paper’s design. The paper found that this residual information was associated with future returns and analyst forecast errors. 

## One more major warning: point-in-time discipline

The NYU paper correctly trains, validates, and tests chronologically rather than randomly mixing time periods. It explicitly warns that random cross-validation would allow future events to influence models evaluated on the past. 

For Duckets:

* Use the filing or accepted date—not merely the fiscal-period end date.
* Never attach a financial result to market bars before it became public.
* Preserve amended filings rather than silently rewriting history.
* Avoid using today’s restated historical database as though investors knew those values originally.
* Keep the model’s training cutoff and feature-availability timestamp in every output.

The NYU paper also emphasizes that detailed XBRL data can contain errors, unnecessary custom tags, and comparability problems.  That reinforces the need for source lineage and confidence scoring.

## Verdict

These are not just loosely related research papers. They offer a strong blueprint for the next Duckets calculation:

```text
Detailed corporate statements
→ levels, changes, and economic interactions
→ probability of fundamental improvement
→ point-in-time fundamental score
→ confidence- and freshness-adjusted fusion with market regime
```

The best first version is **not yet an ML model**. It is an auditable `fundamental-direction` calculation inspired by the papers’ strongest and most repeatable findings—especially earnings, operating cash flow, common equity, taxes, accrual quality, and changes in those values.

The next logical PR is to expand statement fetching to annual and quarterly histories, add a point-in-time corporate feature builder, and register `fundamental-direction` as the second calculation without changing the existing market-regime score.
