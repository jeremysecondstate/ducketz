# Ducketz Loops System Map

Audited baseline commit: `3fdeca189feffb1d8167f67845503fe7cfb183e1`

OPRA-first implementation update: 2026-08-14

The owner numbering is unchanged. “Loop 3” and “Loop 4” mean the logical
Active Pricing and Options Capture owners below; the local Pricing worker is a
child of Active Pricing, not another independent owner.

The authoritative production option universe is exactly `AAPL, AMZN, GOOG,
MU, NVDA, SNDK`. CALL and PUT are derived for each symbol, yielding 12 required
routes. `SPY` is a separately declared research benchmark and cannot enter Loop
A, Loop B, Strategy, readiness, or a production route count.

```mermaid
flowchart LR
  subgraph Providers["External providers"]
    DB["Databento<br/>equities, CME, OPRA.PILLAR"]
    FMP["Financial Modeling Prep<br/>fundamentals, Treasury curve, dividends"]
    ALF["FRED / ALFRED<br/>causal macro vintages and rate fallback"]
    SCH["Schwab Market Data API<br/>equities, chains, execution validation"]
    SEC["SEC filing documents"]
  end

  subgraph Owners["Production runtime owners and owned worker"]
    CME["1. CME/L2"]
    A["2. Loop A"]
    P["3. Active Pricing"]
    W["Active Pricing child worker<br/>provider materialization + finite-basis model"]
    O["4. Options Capture"]
    B["5. Directional Loop B"]
    S["6. Strategy"]
  end

  subgraph Store["Immutable datastore and publication boundaries"]
    LA["Loop A bars/features/readiness<br/>including current ALFRED macro coverage"]
    RR["pools/rates/treasury-curve/fmp/<receipt>"]
    DV["stocks/<SYMBOL>/corporate-actions/dividends/fmp/<receipt>"]
    OE["ml/option-pricing-evidence/opra/<import>"]
    OS["stocks/<SYMBOL>/options/snapshots/<provider>/<target_ns><br/>provider-neutral v2 receipt"]
    PT["option-pricing-target-outcomes<br/>prediction clocks + receipt"]
    PW["loop-native provider materializations<br/>finite-basis model generations"]
    PR["option-pricing-runs<br/>append-only v3 surfaces and receipt chain"]
    BR["ml/runs<br/>Loop B samples/predictions/evaluations"]
    SR["ml/strategy-runs<br/>shadow execution/strategy evidence"]
  end

  DB ==> CME
  DB ==> A
  DB == "OPRA definitions, CBBO, optional quality/reference" ==> O
  FMP ==> A
  FMP == "already-fetched Treasury/dividend payloads" ==> RR
  FMP == "already-fetched declared dividends" ==> DV
  ALF ==> A
  SCH ==> A
  SCH == "broker chain / fallback / execution validation" ==> O
  SEC ==> A

  A --> LA
  LA -. "exact-target readiness" .-> P
  RR --> P
  DV --> P
  OE --> W
  O --> OS
  OS --> P
  P --> PT
  P -. "launches after target publication" .-> W
  OS --> W
  W --> PW
  PW --> P
  P --> PR
  PR --> B
  LA --> B
  OS --> B
  B --> BR
  PT --> S
  PR --> S
  OS --> S
  BR --> S
  S --> SR
  PT -. "exact target barrier" .-> O

  classDef provider fill:#fff3cd,stroke:#9a7500,color:#1f1f1f;
  classDef owner fill:#dbeafe,stroke:#2563eb,color:#111827;
  classDef artifact fill:#ecfdf5,stroke:#059669,color:#111827;
  class DB,FMP,ALF,SCH,SEC provider;
  class CME,A,P,W,O,B,S owner;
  class LA,RR,DV,OE,OS,PT,PW,PR,BR,SR artifact;
```

Provider precedence for fair-value evidence is `databento-opra`, then explicit
causal `schwab` fallback. Schwab remains the broker-enrichment, disagreement,
execution, and fill-validation lane. Offline OPRA imports are permanently
research-only and cannot increment prospective counts.

All arrows represent persisted, receipt-verified data. Ordinary publication is
atomic and monotonic. A generation is invisible before its verified first
availability, after expiry, on quality failure, or when its checksum/receipt
cannot be verified.
