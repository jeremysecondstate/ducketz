# Duckets fundamental-technical lifecycle

This layer combines point-in-time `fundamental-direction` with cross-timeframe
`market-regime` values without hiding disagreement inside one weighted average.

## Output and identity

```text
DATASTORE/stocks/<SYMBOL>/signals/fundamental-technical-lifecycle/consensus/daily.parquet
```

The file has exactly one Duckets-generated identifier:

```text
id = timestamp
```

Inputs are aligned by readable symbol and timestamp values. There are no
observation, feature-set, source-snapshot, or signal IDs.

Loop A rebuilds the signal after fundamentals and technicals unless
`--skip-signals` is supplied.

## Technical consensus

Providers are combined inside each timeframe before timeframe weights are
applied, so two providers covering the same horizon do not double-count it.

| Timeframe | Base weight |
| --- | ---: |
| 5m | 5% |
| 30m | 10% |
| 1h | 20% |
| 1d | 35% |
| 1w | 30% |

Each observation becomes available at the end of its canonical timeframe.
Confidence and freshness reduce its effective weight as it becomes stale.

## Lifecycle phases

- `CONFIRMED_EXPANSION`: fundamentals and technical consensus are strong.
- `EARLY_ACCUMULATION`: fundamentals are strong while technicals are weak but
  turning upward.
- `LATE_CYCLE_DISTRIBUTION`: technicals remain strong while fundamentals are
  weak or deteriorating.
- `CONFIRMED_CONTRACTION`: fundamentals and technical consensus are weak.
- `RECOVERY_ATTEMPT`: technicals improve before full fundamental confirmation.
- `TRANSITION_MIXED`: no decisive confirmation or divergence.
- `INSUFFICIENT_DATA`: no point-in-time fundamental observation is available.

Important value columns include:

```text
fundamental_score
fundamental_change_1q
fundamental_acceleration
technical_consensus_score
technical_consensus_change_5d
short_term_technical_score
long_term_technical_score
technical_term_spread
fundamental_technical_spread
agreement_strength
divergence_strength
lifecycle_confidence
lifecycle_phase
setup_quality
timing_score
```

Run directly:

```powershell
python -m signals.main MU --datastore-target pc
```
