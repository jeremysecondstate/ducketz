# Opening gap versus the first trading hour

Reviewed September 14, 2026, before the scheduled 21:05 Pacific preparation run.

## Finding

The overnight/weekend context forecast and the 04:00–05:00 forecast already share the same hourly training process and fitted model. They ask about different price movements. Combining their target windows would change the direction being predicted, without making a trade entered at 04:00 capture the earlier overnight movement.

The saved hourly cohort contains 64,306 examples: 2,250 opening-gap examples and 62,056 execution-hour examples. The model report confirms both gap and opening-hour examples in the actual fitted partitions for all eleven symbols. AAPL's two published rows both use `models/1h/model.joblib` and the same selected model family. The model uses the route and known calendar characteristics to distinguish the targets. The gap probability is not separately passed into the opening-hour prediction as an input.

Source: [hourly model fitting](C:/dev/ducketz/ml/nightly_gameplan.py:1160), [training-label construction](C:/dev/ducketz/ml/independent_stock_targets.py:153), and the saved [model reports](C:/DATASTORE/ml/nightly-gameplan-runs/20260914T104432.295244Z/model-reports.json).

## Why merging the return windows changes the trade signal

Illustrative prices:

| Time | Price |
| --- | ---: |
| Previous session 17:00 | $100 |
| Next session 04:00 | $110 |
| Next session 05:00 | $108 |

The proposed combined window rose 8%. A purchase at 04:00 followed by a sale at 05:00 lost about 1.82%. An overnight increase can therefore give the combined window an upward direction while the tradable hour moves downward. Tonight's plan is also created after the previous 17:00 baseline; it cannot create an entry at that earlier price.

A read-only comparison of the saved historical cohort found 2,114 symbol/date pairs with both gap and opening-hour observations. In 788 pairs the prior-close-to-05:00 return and the 04:00–05:00 return had opposite signs; 449 had a positive combined return but a negative first-hour return. This compares historical target definitions, not the accuracy or profitability of a newly trained model. No new model was fitted.

Sharing training examples can help the model learn related patterns, but does not establish that either target produces more accurate future predictions. That would require a separate comparison on unseen dates using the actual trade return as the outcome.

## Tomorrow's schedule

The native September 15 grid contains the same structure on an ordinary weekday:

- September 14 17:00 → September 15 04:00: opening-gap context.
- September 15 04:00 → 05:00: first actionable hourly forecast.
- September 15 05:00 → 06:00: next actionable hourly forecast.

The context row adds an output from the existing fitted model; it does not start another standalone model-training job. It is excluded from live entry instructions.

The existing overnight task remains ACTIVE at 21:05 Pacific. This review made no changes to training, target windows, saved forecasts, trading controls or schedules. The first trade continues to be judged against its actual 04:00 entry window. The proposed clock merge was not applied because it would mix a different return into that trade's direction.

Source: [stock target windows](C:/dev/ducketz/ml/independent_stock_targets.py:70) and [execution signal selection](C:/dev/ducketz/ml/stock_trader/gameplan_execution.py:43).
