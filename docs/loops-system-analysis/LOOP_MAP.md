# Current Loops map

```mermaid
flowchart LR
    D["Existing DATASTORE + providers"] --> A["After 17:00 PT<br/>fetch and append completed session"]
    A --> B["Build directional data and models"]
    B --> E["Evaluate all saved Gameplans<br/>keep forecasts pending until mature"]
    E --> T["Train Strategy models"]
    T --> S["Generate Strategy candidates"]
    S --> G["Train and save next-session Gameplan<br/>144 forecasts + 144 intents"]
    G --> P["Saved Gameplans<br/>September 4 onward"]
    P --> E
    G --> U["Duckets forecast display"]
    G --> H["04:00–17:00 PT<br/>hourly stock trader"]
    H --> R["Current quotes, broker session,<br/>risk and exact-once checks"]
    E --> W["Saturday review<br/>evaluated and pending forecasts"]
    O["Overnight Scheduled supervisor<br/>read progress/errors; repair and resume"] -.-> A
    O -.-> B
    O -.-> T
    O -.-> S
    O -.-> G
    K["Ten-minute health watch<br/>one supervision owner at a time"] -.-> O
```

## Authority rules

- Solid arrows show workflow/data dependencies; dashed arrows show supervision.
- OPRA history is Loop A-owned stage work, not a separate scheduled loop.
- Completed-session quote evidence is valid for overnight planning even when
  hours old after close.
- A future live option execution must revalidate the same frozen legs and can
  execute or skip only.
- The live daytime stock reader does not fetch, train, or replan. It may place
  only a risk-gated stock order when both activation controls and `--execute`
  are present; the paper reader remains broker-free.
- The Duckets UI reads the immutable pointer directly and rotates display rows
  on hourly clock boundaries; it does not require the daytime reader schedule.
- Realized option P/L is not inferred from a frozen intent; it requires an
  exact-leg execution receipt. Unexecuted studies must be labeled
  counterfactual.
