# Saturday Gameplan review

Reviewed October 11, 2026: this remains the retained forecast-evaluation contract.
Atlas's saved `Loops Gameplan Weekly Review` definition is **paused**, with a
retained Saturday 09:00 America/Los_Angeles rule. That recurrence is configuration,
not proof of a current wake or completed review; Scout requires its own readback.
See [current task observations](CURRENT_SYSTEM.md#atlas-task-observation-on-october-11).
The stable path remains referenced by the saved task.

The evaluator starts with the first Gameplan, **September 4, 2026**. The first
scheduled September 5 review was intended to cover Friday's plan only. This weekly Gameplan
evaluation is distinct from the named Loop C options/risk observer and its paper
ledger, regardless of historical task naming. It does not import earlier
predictions, refresh a paper ledger, create risk proposals, contact a broker,
or retrain models.

For an authorized review, the retained entry point from this checkout is below.
It was not run for the documentation audit.

```powershell
.\.venv\Scripts\python.exe -u -m ml.gameplan_evaluation --datastore-target pc
```

The evaluator reads every receipt-verified Gameplan from the starting date and
matches each forecast to its exact observed target window. It saves pending
forecasts and revisits them later, even after the latest Gameplan advances.
Already scored forecasts retain their scores. Missing/corrupt saved evidence
is an error, not permission to silently omit a plan.

Use `ml.gameplan_evaluation.read_evaluation_history` to verify
`ml/gameplan-evaluation-latest/run.json` under the configured datastore and its
selected
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
