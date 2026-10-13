# Scout 15m manual-start runbook

Scout now defaults to the local broker-account trader, following the October 13
human correction. Use the [current manual-start instructions](scout-local-15m-manual-start.md).

```powershell
.\.venv\Scripts\python.exe -B -m ml.intraday.scout_prestart
.\.venv\Scripts\python.exe -B -m ml.intraday.live_session --live --scout-admission broker-local
```

The first command checks the local installation without contacting providers or
starting anything. The second is the human's explicit real-order start. Atlas
enrollment, its address and `--check-connection` are not prerequisites for this
mode. Existing distributed admission is separately available through
`--scout-admission atlas-service`; its authenticated connection check applies
only when that mode is explicitly selected.

Keep installed readiness, offline validation and actual human-started runtime
results separate. The current instructions document quantity adjustment, local
recovery, accounting limits and the absence of atomic cross-PC reservations.

The current instruction is standing for every night: Atlas plan + Scout plan,
Scout synthesis, then delivery to Atlas completes the Gameplan handoff. No
human confirmation, third snapshot, peer acceptance or cross-trader readiness
is required. Each trader executes independently and keeps its own logs.
