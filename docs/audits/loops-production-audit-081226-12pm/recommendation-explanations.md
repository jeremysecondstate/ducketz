# Recommendation Explanations

- **Rec 1:** Keep a dependable core feature set for every model, and enable optional data families for a horizon only when its past training data is sufficiently complete. Treat indicators for missing data as candidate features that must prove useful in the same time-based tests.

- **Rec 2:** Combine every verified pricing-surface generation into a time-indexed history that is never rewritten, with enough provenance to establish when each record first became usable. Loop B can then join each historical sample to the latest surface that was actually available at that time.

- **Rec 3:** Choose the model and probability-calibration method independently for each forecast horizon using only earlier time windows. Freeze that choice before the final test period and deploy it only if it beats a simple historical-rate predictor.

- **Rec 4:** Save each verifiable realized spread outcome as soon as its entry and exit evidence becomes available, rather than reconstructing the training set from scratch. Start fitting the profitability model only after enough complete, independent outcomes have accumulated; until then, continue using scenario-based rankings.

- **Rec 5:** Label each historical decision using an execution-cost estimate derived from the bid/ask and liquidity visible at that moment instead of applying the same cost everywhere. Keep the current fixed cost only when a usable quote is unavailable, and version the new label definition for reproducibility.

- **Rec 6:** Begin Loop A immediately at the 15-minute boundary and wait only where a particular provider or bar actually needs extra settlement time. This removes a blanket delay from work that is already ready to run.

- **Rec 7:** Publish the small pricing dataset needed by Loop B as soon as its causal inputs are computed, then let heavier research and reporting continue separately. Link the early and full publications with the same target identity and checksums so consumers can verify their relationship.

- **Rec 8:** Store completed historical feature joins under keys that capture their exact inputs and configuration. On later cycles, reuse those unchanged rows and rebuild only the decisions affected by newly published data.

- **Rec 9:** Continue fetching fast-changing bars and quotes every 15 minutes, but refresh statements, economic series, and filings only when their release schedules or saved progress indicate new data may exist. Reuse the most recent verified slow-moving artifact between those refreshes.

- **Rec 10:** Maintain a durable index showing which strategy samples are still waiting for evidence and which already have final outcomes, identified by their exact source receipts. Each cycle then revisits only samples that may have become complete and directly reuses all finished results.
