# Ducketz Loops system mind map

```mermaid
mindmap
  root((Ducketz))
    Overnight 17:05 PT
      Loop A close fetch
        EQUS equity continuation
        OPRA definition
        OPRA cbbo-1m
        OPRA ohlcv-1h
      Directional Loop B
        causal feature rows
        stock path evidence
      Strategy-profit ML
        1h
        4h
        1d
        1w
        histogram gradient
        MLP neural challenger
      Strategy candidates
        exact contracts and legs
        completed-session planning quotes
      Immutable gameplan
        144 forecasts
        144 options intents
        receipts and checksums
    Daytime 04:00–17:00 PT
      frozen plan reader
      hourly stock-only trader
      current broker session and risk checks
      no fetch or training or replan
      options remain non-executable
    Supervision
      inspect progress and errors while running
      repair and resume failed stages
      ten-minute backup health watch
      healthy fits may run for hours
      next-session 04:00 deadline
    Saved Gameplans from September 4
      keep longer forecasts until mature
      Saturday review
    Next close
      fetch completed day
      evaluate all saved directional forecasts
      option P/L requires exact-leg execution evidence
      build successor plan
    Safety
      latest completed session is overnight current
      live quote gate is separate
      same legs execute or skip
```

The detailed authority contract is `NIGHTLY_GAMEPLAN.md`. The former SVG and
editable Mermaid assets describe the legacy recurring topology and are retained
only as historical design artifacts; this inline diagram is current.
