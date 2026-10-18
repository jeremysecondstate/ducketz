# Loops responsibility map

```mermaid
flowchart TD
    D[Completed-session data and readiness] --> S[Score original forecasts into Stats]
    S --> R[Bounded model review]
    R --> G[Local fitting and immutable Gameplan]
    G --> P[Enrichment and local planning inputs]
    P --> E[Verified private owner packages]
    E --> C[Scout combined capital plan and Stats]
    C --> H[Atlas verifies frozen handoff]
    C --> U[Scout verified readers]
    H --> V[Atlas verified readers]
    H --> X[Separate execution readiness]
    M[Human trader start and stop controls] --> X
    X --> T[Atlas stock execution and actual fills]
    G --> F[Original forecast history]
    F --> S
    O[Responsibility recovery with saved owners and receipts] -.-> D
    O -.-> P
    O -.-> E
    A[Accepted Strategy and pooled sequence evidence] --> Q[Independent Loop C options observer]
    Q --> L[Counterfactual paper ledger and operator review]
```

Solid arrows show dependencies, not automatic authority or a timetable. The
Loop C lane is independent of stock readiness and does not control the stock
worker. Independent stock horizons and the separate pooled encoder
retain their own contracts. Optional options/CME/ALFRED modules remain supported
inputs where the selected scope requires them.

See [Current system](CURRENT_SYSTEM.md) for October 11 Atlas task observations,
[relationships](LOOP_RELATIONSHIPS.md) for artifact boundaries and
[research onboarding](RESEARCH_SYMBOL_ONBOARDING.md) for explicit candidate
admission. Research selection and schedule installation do not start a trader.
