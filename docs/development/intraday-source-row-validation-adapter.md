# Source-row validation reuse for existing adapters

The source publication base does not contain the fuller source-population and
recovered-population modules. This change publishes only their exact narrow
compatibility patch and offline evidence. It does not add provider acquisition,
change data-population policy, or install one PC's model recipes on another.

`verified_source_observation` still checks every retained native observation.
It constructs the fixed one-minute duration once per invocation, reuses the
already parsed native opening clock, and parses each native availability clock
once. Source identity, provenance, quarter/minute alignment, ordered distinct
observations, coverage, OHLCV and availability limits retain their existing
checks and errors. No validation or original row is cached between calls.

`verified_recovery_candle` still rebuilds the expected candle through its
original primary, alternate-observation or explicit-derived-evidence path.
New candles return plain JSON values through `_wrapper`; an already wrapped
primary returns `_plain(candle)`. The validator
normalizes the actual caller row and compares it with that already canonical
expected value, avoiding one repeated encoding/decoding of the same evidence.
Nullable native clocks, NumPy/Pandas caller values, full evidence and every
contract-owned field keep their original handling.

Apply [the exact patch](patches/intraday-source-row-validation.patch) only to an
existing reviewed installation, preserving its other writers' changes and its
own scientific population/model policy. The patch uses LF source lines. Review
an installation's line endings and compose the exact hunks; retain original
operating bytes and record the installed result separately.

| Existing module | Original SHA-256 | Adapted SHA-256 |
| --- | --- | --- |
| `ml/intraday/coverage.py` | `2a249d2caa57026e12ff7933f453bbf50aa3612612af63fd623d62a9a24f5bd3` | `7c549b2dcd5ea750fda1762b4fe9c6bbd1d4b154b601ffbcb667720abe93b11a` |
| `ml/intraday/recovered_population.py` | `3b263de6be4b964ce8cb4dd36f36ec92a0893f9b3e4f02a83093d6b5735cee9f` | `268351c7138645529faf0b6acad571a8a46b2ec3b0c7d145df1971bb39136b2d` |

Patch SHA-256: `b38785ed947b7dce2ae398a206ea9a05d6887be424901fb3dce7533a673c1340`.

The test-only fragments under `tests/fixtures/intraday_source_row_validation`
are exact extracted original method and helper bodies. Their manifest records
the original full-module identity, source lines and segment hashes. They have no
provider, account, datastore or execution entry point. The tests apply the
published patch to the authentic before fragments and check their exact adapted
hashes, then exercise the original and patched validators against genuine
constructed fixture evidence and deliberately malformed variants. Native
provenance validation uses the exact retained helper; it is never stubbed out.
The recovery paths use the candidate checkout's existing gap-evidence logic.

Run `python -B tests/check_isolated_intraday_source_row_validation_adapter.py` from the reviewed checkout. The wrapper denies network
and subprocess calls, isolates imports from editable installations, and records
actual imported code plus every patch/fixture/document fingerprint. The local
installation additionally needs its own existing source-population, recovery,
data, sizing and source-evidence checks and exact retained-science comparison.

These offline results prove the removed repeated work and covered source
behavior. Source publication, local installation, process loading and a
naturally affected live cycle remain separate facts. A future human-chosen
ordinary manual start loads installed source; this change never starts,
restarts, stops or places orders through a trader. Historical originals,
scientific labels/probabilities/targets and all manual controls remain intact.
