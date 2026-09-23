# Current CME source review

Reviewed 2026-09-23T04:19:39.881246+00:00. **No new native request blocker evidenced.**

The ESU6/NQU6 unresolved-symbol warning matches the previous run, and the four explicit source settings are unchanged. All six CME requests completed with `status=ok`. All five continuous roots appear in all three current schemas; the continuous MBP response remains capped at 5,000 rows.

**Newly disclosed limitation:** CLV6 has no current raw BBO or MBP observations; only GCZ6 returned in those scopes. CLV6 remains present in raw OHLCV, last observed September 22 at 18:29 UTC. The reason is not established by this local review. No expiry, failed download, source substitution or guaranteed complete raw-contract coverage is inferred.

Normalized current-stage row counts differ from total fetched response counts when existing observations are retained during deduplication. Sparse observations alone do not demonstrate download failure.

The current optional derivation verdict is pending; the latest diagnostic inspected still belongs to the preceding run. Complete Loop A verification remains required. No provider call, retry, configuration/source write, process change or lock action was performed.

[Exact evidence](C:/dev/ducketz/artifacts/analysis/overnight-20260923/cme-progress-review.json)
