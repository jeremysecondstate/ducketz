# Saturday Gameplan review

`Loops Gameplan Weekly Review` runs Saturday at 09:00 America/Los_Angeles.
The review starts with the first Gameplan, **September 4, 2026**. Saturday
September 5 therefore reviews Friday's plan only. It does not import earlier
predictions, refresh a paper ledger, create risk proposals, contact a broker,
or retrain models.

From `C:\dev\ducketz`, run:

```powershell
.\.venv\Scripts\python.exe -u -m ml.gameplan_evaluation --datastore-target pc
```

The evaluator reads every receipt-verified Gameplan from the starting date and
matches each forecast to its exact observed target window. It saves pending
forecasts and revisits them later, even after the latest Gameplan advances.
Already scored forecasts retain their scores. Missing/corrupt saved evidence
is an error, not permission to silently omit a plan.

Use `ml.gameplan_evaluation.read_evaluation_history` to verify
`C:\DATASTORE\ml\gameplan-evaluation-latest\run.json` and its selected
manifest, receipt, and outputs. The run contains `evaluations.parquet`,
`summary.json`, and `review.md`. Report:

- Review week and included Gameplan dates.
- Evaluated, not-yet-mature, and mature-but-missing-data counts.
- Direction accuracy and mean Brier score when outcomes exist, with sample size
  and separate model group/promotion-status results. Missing scores are not zero.
- Report/receipt paths and missing outcome data or corrupt inputs.

This reviews predictions, not brokerage profit. Options intents are not assumed
to be executed trades. The command places no orders and changes no Gameplan.
If overnight work is still building data, describe the current coverage and
pending outcomes. Its evaluator will revisit them; do not interrupt healthy
training to finish the Saturday review.
