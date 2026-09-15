# September 15 planning-policy provenance

Recorded: 2026-09-15 04:17:15 UTC (September 14, 21:17:15 PDT).

## Finding

The installed four-hour sparse-session planning policy follows a September 14 user request to handle the CROX/TWST gaps that exceeded the older fifteen-minute rule. The **240-minute limit was the assistant's disclosed implementation choice**, rather than a separately specified numeric limit in the user's message. The user subsequently acknowledged the result and requested a display fix. This is later evidence than the fifteen-minute language retained in the overnight automation prompt; it is separate from the expired September 14 opening/deadline exceptions.

## Direct conversation evidence

- Task title: **Brainstorm weekly stock research**.
- Task ID: `01a09501-ebb0-7693-a284-d0ea6bee7df8`.
- Relevant turn: `01a09fe7-ab99-7973-a607-c1f712d12830`.
- Turn began **September 14, 05:32:35 PDT / 12:32:35 UTC** and completed **05:48:01 PDT / 12:48:01 UTC**.
- User message ID: `01a09fe7-abf3-7b40-9d9e-0ea2965e921c`.
- Retrieved read-only through `mcp__codex_app__read_thread`; no task was continued or modified.

The user annotated the preceding explanation that CROX's last Friday close was 143 minutes before the required boundary, TWST's was 48 minutes before, and the existing planner allowed only fifteen minutes. Their actual instruction was:

> I feel like we should handle these sorts of symbol-specific data issues. Like a front-fill or some other appropriate manner to fill up the missing areas. Sometimes the reason for the missing data like this is because simply no trades were made at that time. If I'm understanding the missing bar situation correctly? It's OHLCV bars?

During that turn the assistant explicitly disclosed:

> I’ll cap that carry-forward at four hours—the normal after-hours period—and require a price from the same session. Historical entry prices will still come from actual bars.

This is assistant message `msg_009c2702dbdac558016aa7e9dd3d3c87d0a9e7c93121eab217`. The final response, `msg_009c2702dbdac558016aa7ecfe18e887d087474f348431f37d`, reported a four-hour after-hours cap, verified same-session download coverage, synthetic/assumed-zero-volume labeling, original observation timestamps, and the same rule for historical planning closes. It stated that native data, training labels and live quote freshness retained their existing rules.

The next user turn, `01a09ffa-a4e2-7e10-8198-ad2e6b236718`, began **September 14, 05:53:18 PDT / 12:53:18 UTC**:

> Sweet! It looks like there might be a display error, because none of the symbols when individually selected from the dropdown displays (see screenshot with red markup).

This acknowledges the preceding result and asks for a UI repair; it should not be paraphrased as a separate explicit user statement of the 240-minute bound.

## Saved implementation evidence

The historical informational review is [Gameplan.md](/C:/DATASTORE/ml/gameplan-trade-plan-runs/20260914T124427.308015Z/Gameplan.md). A bounded metadata read found:

- Receipt `COMPLETE`, completed **September 14, 05:46:15.957610 PDT**; action date September 14.
- Source remains `ml/nightly-gameplan-runs/20260914T104432.295244Z`.
- Manifest configuration: `allow_sparse_session_references=true`.
- Price-path contract: `conditional-hourly-planning-price-path-v3`.
- Completion contract: `sparse-session-planning-reference-completion-v2`, `max_gap_minutes=240`.
- CROX: actual close **September 11, 14:37 PDT**, carried through 17:00, **143 minutes**.
- TWST: actual close **September 11, 16:12 PDT**, carried through 17:00, **48 minutes**.
- Both references retain XNAS.ITCH lineage and `ASSUMED_NO_TRADES`; the synthetic effective boundary does not replace the actual observation timestamp.

[Saved validation](/C:/dev/ducketz/artifacts/analysis/sparse-session-planning-20260914/validation.json) records 264 forecasts, eleven symbols, all 154 hourly planning points available, complete cash projection, twelve projected events, preserved source/prior-review receipts and original account snapshot, and zero review orders. This provenance follow-up read the recorded evidence; it did not rerun that historical validation or imply fresh checksum revalidation.

[RESEARCH_SYMBOL_ONBOARDING.md](/C:/dev/ducketz/docs/loops-system-analysis/RESEARCH_SYMBOL_ONBOARDING.md:235), [NIGHTLY_GAMEPLAN.md](/C:/dev/ducketz/docs/loops-system-analysis/NIGHTLY_GAMEPLAN.md:245), and [INDEPENDENT_STOCK_HORIZONS.md](/C:/dev/ducketz/docs/loops-system-analysis/INDEPENDENT_STOCK_HORIZONS.md:252) document the subsequent sparse-session policy. Current native entry/actuals/training observation tolerances remain separate from the planning closing-reference assumption.

## Separate evening direction-policy change

The [overnight automation memory](/C:/Users/7980X/.codex/automations/loops-hourly-operations/memory.md), section **User-approved removal of neutral band — 2026-09-14T21:04:01.504433-07:00**, records the user's later request to remove the 46%–54% neutral band. It records `stock-direction-50-v2`: above 50% bullish, below 50% bearish, exactly 50% neutral, while preserving historical frozen forecasts and existing execution behavior. The supporting artifact is [direction-policy-review.md](/C:/dev/ducketz/artifacts/gameplans/2026-09-14/direction-policy-review.md).

This direction-policy provenance is from the saved memory and supporting artifact reference, not a second direct conversation retrieval in this follow-up. It is independent of the morning sparse-session price policy and supplies no extension of the September 14 deadline or late-opening exceptions.

## Scope of this follow-up

Only this evidence note was created. No source code, automation, production artifact, trading control, supervision claim, provider/broker request, or running process was changed.
