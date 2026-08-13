# Recommendation Explanations

Ranks are overall implementation priorities (1 = highest), based on expected Loop-wide impact, strength of the audit evidence, dependency leverage, and benefit relative to effort.

- **Rec 1 — Rank 2/10:** Loop B currently sends nearly empty optional feature families into preprocessing, so imputed noise can destabilize horizon models and obscure whether a data family adds signal. Coverage gates keep every horizon on a reliable causal baseline and admit CME, options, pricing, macro, SEC, or other features only when enough point-in-time history shows that they improve out-of-sample predictions.

- **Rec 2 — Rank 1/10:** Loop B currently joins historical decisions to only the latest Pricing publication, leaving historical pricing features empty or temporally wrong. An append-only, receipt-verified surface history gives every sample the surface actually available at its cutoff, increasing causal `opx__` coverage and making training resemble live inference.

- **Rec 3 — Rank 9/10:** The same logistic model and Platt calibrator are forced on all horizons even though their signal shapes and sample sizes differ, and most audited horizon accuracies were below 50%. Horizon-specific temporal selection can improve probabilities and calibration, while the untouched final window and prior-baseline gate prevent a model from being deployed unless its apparent gain generalizes.

- **Rec 4 — Rank 3/10:** Strategy currently has no receipt-complete spread outcomes, so it cannot train a profitability model and all candidates remain scenario-ranked. Incrementally preserving verified entry and exit outcomes lets Strategy begin learning realized profitability as soon as the evidence threshold is met, while keeping the scenario path safe until then.

- **Rec 5 — Rank 8/10:** Using a flat 10-basis-point cost gives cheap and expensive trades the same label, so Loop B may call a move profitable even when the visible spread would have consumed it. Point-in-time liquidity costs align labels and predicted positives with trades that could actually clear their observed execution hurdle, while the fixed cost remains a fallback.

- **Rec 6 — Rank 10/10:** Loop A currently makes all work wait 20 seconds after every quarter-hour even when its data is ready. Starting the fast lane immediately and applying settlement waits only to sources that need them moves Loop A, Pricing, Loop B, and Strategy publications up by as much as 20 seconds per cycle.

- **Rec 7 — Rank 4/10:** Loop B waits for Pricing's full research and reporting tail, so it can miss the current boundary's surface and consume an older one. Publishing the compact causal surface first gives Loop B same-boundary pricing sooner and advances Strategy, while checksums retain a verifiable link to the later full generation.

- **Rec 8 — Rank 5/10:** Loop B currently rebuilds its entire multi-horizon sample history every cycle even though most rows and source artifacts have not changed. A receipt- and configuration-keyed cache limits work to affected decisions, cutting I/O, CPU, memory pressure, and Loop B-to-Strategy latency as history grows.

- **Rec 9 — Rank 6/10:** Loop A polls slow-moving statements, macro series, and filings every 15 minutes, delaying completion and consuming provider and datastore capacity when no new release exists. Evidence-driven refresh schedules reuse the last verified artifacts between releases, leaving the fast cadence for bars and quotes and freeing the rest of the Loop pipeline sooner.

- **Rec 10 — Rank 7/10:** Strategy repeatedly scans historical Loop B samples and option snapshots—including completed or still-impossible outcomes—on every cycle. A durable receipt-keyed status index revisits only samples with newly available evidence and reuses final outcomes, reducing chain reads and keeping Strategy runtime from growing with the full history.
