# Schwab authorization blocker — September 24 overnight

Observed at **2026-09-24 04:25:01 UTC** (September 23, 21:25 Pacific).

The shared native cache still requires reauthorization. No newer successful
authorization was present at this observation time:

| Safe metadata | Value |
|---|---|
| Cache path | `C:/dev/ducketz/data/schwab_tokens.json` |
| Last successful save | `2026-09-24T03:11:31.089898+00:00` |
| Access expiration | `2026-09-24T03:40:31.089898+00:00` |
| Rejected refresh marker | `2026-09-24T04:22:15.444389+00:00` |
| Marker status | `FAILED_REAUTH_REQUIRED` |
| Allowlisted error | `invalid_grant` |
| Reauthorization required | `true` |
| Uncertain refresh outcome | `false` |

The code correctly suppresses another OAuth POST using the rejected cache.
[The pre-POST guard](C:/dev/ducketz/app/services/schwab.py:183) checks the persisted
rejection before [the refresh POST](C:/dev/ducketz/app/services/schwab.py:203).
[The rejection handler](C:/dev/ducketz/app/services/schwab.py:753) saves the
allowlisted rejection durably. [The native reader](C:/dev/ducketz/app/services/schwab_token_store.py:202)
recognizes `FAILED_REAUTH_REQUIRED` and `invalid_grant`.

The native fetch log contains one initial HTTP 400 OAuth failure message and
22 local reauthorization-required messages. These repeated local refusals do
not establish repeated OAuth POSTs. The inspected guard and durable state show
why later symbol requests fail locally. Existing regression coverage is
[test_rejected_refresh_requires_reauthorization_without_duplicate_post](C:/dev/ducketz/tests/test_schwab_portfolio.py:978);
no new test or live credential experiment was needed for this diagnosis.

The required recovery is the user-owned local command
`.\.venv\Scripts\python.exe -m app.main schwab-auth`, whose entry point is
[app/main.py](C:/dev/ducketz/app/main.py:8). A successful code exchange writes a
fresh payload without the rejection marker. Before resuming native overnight
work, verify a newer `saved_at` and a cleared rejection/uncertainty marker.
Do not retry the unchanged rejected credentials.

This inspection performed no network, broker, account, order, cache, lock, or
production writes. It used the native read-only payload primitive with stable
file metadata before and after reading, avoiding the locking wrapper's cleanup
and permission-update side effects. Only explicitly allowed timestamps, flags,
paths, and the allowlisted error were emitted; no token or client values were
printed or saved.

Evidence: [safe JSON diagnosis](C:/dev/ducketz/artifacts/analysis/overnight-20260924/auth-diagnosis.json)
and [native fetch log](C:/DATASTORE/ml/overnight-runs/20260924T040733.615371Z/loop_a_close_fetch.log).
