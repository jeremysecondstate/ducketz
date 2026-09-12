# Scorecard v1

These are initial research-policy choices, not optimized parameters, calibrated
probabilities, or a claim of investment performance. Change the version and
evaluate future outcomes if the policy is revised; do not rescore old calls.

| Dimension | Weight | Evidence expected |
|---|---:|---|
| Valuation | 30 | Present value range, appropriate comparables, priced-in expectations, uncertainty |
| Product economics | 25 | Customer value, paid adoption, retention/repeat purchases, pricing, durable advantage, cash conversion |
| Catalysts | 20 | Dated observable developments in the next twelve months, objective success/failure criteria |
| Financial resilience | 15 | Cash, burn, financing, leverage, maturities, dilution, ability to reach the catalyst |
| Evidence | 10 | Recent source diversity, direct verification, contradictions addressed, reproducible facts |

Each rating is 0-5: 0 means no support or a strong contradiction; 1 weak;
2 mixed; 3 adequate; 4 strong; 5 unusually strong. Every rating and gate needs
a rationale and source IDs. The weighted score is sum(weight * rating / 5).
Evidence uncertainty must affect the evidence score and scenario width.

## Recommendation requirements

- Total score at least 70/100; valuation, product economics, catalysts, and
  evidence must each score at least 3/5.
- Verified US-listed USD equity identity, including CIK and stable share class.
  US ADRs can qualify if their currency and listing satisfy the same checks.
  ETFs and funds are benchmarks, not company recommendations.
- Latest completed regular-session close, reconciled to the archived FMP
  daily record. Twenty-session median daily dollar volume at least $2 million.
- Financial statement reconciliation, primary-source corroboration, a clear
  opposing case, and financing through the catalyst window.
- Emerging companies need at least eighteen months of conservatively modeled
  cash runway. Include scheduled obligations, capex, and dilution. Anticipated
  uncommitted financing does not count as available cash.
- At least four sources spanning financial filings, company materials,
  independent evidence, and market data. Several articles repeating one press
  release are one underlying claim, not independent corroboration.
- Estimated discount to base present value at least 20% for established or
  30% for emerging businesses. Twelve-month base price upside must separately
  meet the same hurdle. Discount = 1 - current_price / base_present_value.
- At least one future catalyst within twelve months. A five-year technological
  possibility without a nearer measurable milestone is a watch candidate.

Established means an operating business with demonstrated commercial demand;
it does not necessarily mean GAAP profitability. Emerging means commercial
economics, scale, or technical feasibility remain materially unproven.

Recommendations are candidates for the user's investigation. A high score
does not override a failed gate. Watch/pass cases may retain null valuation and
price targets rather than fabricate numbers. Strong products can remain on
watch because the price, financing, evidence, or timing is unattractive.

## Valuation discipline

Compute bear/base/bull cases using either FCFF discounted cash flow or an
enterprise-value comparable multiple. The code calculates value per share;
the analyst owns and explains the assumptions. Use consistent USD-million
financial amounts and million-share diluted counts. Net debt includes debt
and other senior claims minus excess cash and non-operating assets. Reconcile
leases, warrants, minority claims, and R&D/stock-compensation treatment. Do not
count a cash expense and its equity-dilution equivalent twice.

DCF: discount 3-15 annual FCFF estimates, then a stable-growth terminal value.
The terminal cash flow must be positive, terminal growth -2% to +4%, and the
discount rate greater than terminal growth. Discount rate reflects cost of
capital for FCFF. The terminal model must reflect sustainable reinvestment;
do not assume perpetual growth without the capital needed to fund it.

Comparables: enterprise value = forward revenue/EBIT/EBITDA/FCFF * a justified
multiple; subtract net debt and divide by diluted shares. Explain peer
selection and adjustments for margins, growth, risk, and capital intensity.
Relative valuation is explicitly labeled and cannot establish absolute
undervaluation on its own. Cross-check the implied economics and reverse
valuation in the memo; a whole peer group can be expensive.

Six- and twelve-month share-price scenarios are separate from present value.
Explain partial realization of a discount and catalyst failure; never assume
the price converges to intrinsic value by the evaluation deadline. Scenario
prices have no claimed calibrated probabilities.

The local NYU source is `docs/edu/stocks/nyu-strat.pdf`, especially Inputs to
Valuation and Multiples and Companion Variables. Its historical examples and
screening results are methodological context, not current parameters.
