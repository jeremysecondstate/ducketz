# 2026-10-02 Paper two-hour cadence floor

Direct human instruction changed the prospective Paper Improvement ladder to
**one step forward, one step back with a two-hour floor**. A verified WIN adds
one hour; a verified LOSS subtracts one hour but never below two hours; TIE and
UNSCORED retain their committed duration. This is a scheduling and experiment
duration policy, not evidence of a performance improvement.

The native rule is `win-plus-one-loss-minus-one-floor-two-v3`. Historical v1
and v2 assessments keep their recorded semantics. In particular, the consumed
2026-09-29 v2 loss-penalty amendment remains historical and is never reapplied.
An active v2 round retains its immutable seed, duration, due time, assessment,
and comparison. It is not extended or re-scored merely because the floor changed.

The one-time v2-to-v3 transition is part of native `advance`, after the normal
fresh successor is accepted. `advance --adopt-two-hour-floor` requires a unique
change ID and exact ending/successor identities, validates the retained v2
assessment, accepted opening, exclusions, lifecycle locks, and the still-future
successor deadline, then writes an immutable amendment receipt before its state
update. It clamps a one-hour v2 successor to two hours and commits it under v3.
Retries use the same receipt; tampering, a late successor, lock contention, or
wrong identities are rejected.

The scheduler must then receive full native PAUSED then ACTIVE updates with
`FREQ=HOURLY;INTERVAL=N`, where `N` is the committed v3 successor duration. No
automation TOML/database edit, real-account action, Powder change, model retune,
or synthetic fill is part of this policy change.

Focused validation after implementation:

```text
C:\dev\ducketz\.venv\Scripts\python.exe -B -m pytest \
  tests/test_hyperliquid_paper_cadence.py \
  tests/test_hyperliquid_paper_exploratory_cadence.py \
  tests/test_hyperliquid_paper_review.py -q -p no:cacheprovider
```

The suite verifies historical v2 compatibility, v3 loss/win/floor behavior,
immutable prior assessment bytes, idempotent receipt-first recovery, tamper
detection, late-successor rejection, and both cadence/review locks. Runtime
application is recorded separately in the next accepted Paper handoff.
