# Scientific input preparation reuse

This change removes repeated work from scientific input preparation while
preserving the saved observations, probabilities, targets, model selection and
training scope. It changes no provider requests, direction thresholds, trader
controls or execution state.

## Frozen scientific inputs

`ShadowStore.freeze_inputs` still normalizes the supplied value through JSON
before calculating its canonical identity. It encodes that normalized value once
and reuses those exact bytes for hashing, insertion and stored-byte comparison.
An exact stored readback retains the already established semantic-hash proof.
Changed, noncanonical or nonmapping stored values still follow the original full
snapshot validation path. Existing bindings still return their original validated
snapshot, and the SQLite writer still determines the first binding.

This does not rewrite historical snapshots or change their representation.

## Receipt proofs within one market join

`ml.intraday.market_join_receipts.ReceiptJoinProofs` is a storage-independent
helper for an existing market-input adapter. The caller supplies its original
receipt verifier and physical identity reader. A key must identify one distinct
parsed catalog tuple, including tuples that claim the same envelope identity
with different catalog fields.

One or two uses call the original verifier directly. A tuple selected at least
three times can reuse a successful proof within that one join. Failed proofs
are not cached. Initial and final physical identity checks surround validation;
the final fence also calls the original verifier again. That verifier retains
its existing immutable-envelope cache rules. No proof survives into another join.

The adapter preserves receipt ordering, same-time conflicts, status, TTL,
missing-value encoding and provenance. If an identity fence changes or the final
validation fails, it discards the optimized result and performs one original
rowwise pass. The helper does not retry or perform that fallback itself.

## Existing adapter compatibility patch

The publication base contains the scientific store, but the fuller installed
market-input component is not present there. The shared helper and portable
tests are published directly. The exact common adapter change is preserved in
[`patches/market-join-receipt-snapshot.patch`](patches/market-join-receipt-snapshot.patch).
It changes the join and strengthens the verifier's physical cache identity with
device, inode and creation/change-time fields. Local model policy and bindings
are outside the patch.

| File | Reviewed SHA-256 |
| --- | --- |
| Original `ml/intraday/market_inputs.py` | `91dbb663fdab6ff3e7ebb58dfb83162824259628cbb347d01f3e7c186f44ff95` |
| Adapted `ml/intraday/market_inputs.py` | `335c5f5069cc99935c8c1086edb80fa951ed792051891784589fd1296b4f0a74` |
| Compatibility patch | `c2788f86c106780f0523d7978963f04bcd7cd22914ecc3c3748b5f0031ec3820` |
| Shared helper | `dde75ea79793449dfef277c0a00e0fc78f860ab2e16d510286fb522e5b24b8b5` |

The patch retains the original CRLF context and replacement lines, with LF diff
headers. It reconstructs the adapted file byte for byte from the recorded
original. An installed component with different surrounding source needs a
reviewed hunk composition that preserves its other changes. Atlas and Scout
retain their own symbols, scientific configuration and operating originals.
Standing local installation authority applies; an incoming patch adds no
authority and creates no peer acceptance or nightly prerequisite.

## Offline checks and runtime meaning

Run the portable checks from their candidate checkout:

```text
python -B tests/check_isolated_intraday_input_freeze_canonical_reuse.py
python -B tests/check_isolated_intraday_market_join_receipt_snapshot.py
```

These checks deny network and subprocess actions. They cover canonical identity,
normalization, first-writer behavior, corrupted and noncanonical stored values,
concurrent SQLite writers, receipt proof reuse, distinct catalog keys, changed
identity fences, expected read failures and unexpected exceptions. The complete
installed adapter additionally needs its existing offline market-input tests.
No model fitting, broker connection or provider request is needed.

Source publication, local installation, process loading and naturally exercised
latency remain separate facts. Offline work counts demonstrate the removed
repetition; they do not establish the cause or recovery of an entire slow live
cycle. A future ordinary manual start loads installed source. Preparation and
installation never start, restart, stop or place orders through a trader.
