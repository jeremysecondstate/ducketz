# Shared 15-minute storage

Daytime collection, nightly history continuation and model consumers reuse
canonical storage. Retired Gameplan production is not a consumer to recreate.
Local symbols, scientific recipes, original probabilities and targets, execution
state and manual session controls remain owned by each PC.

| Data | Canonical representation |
| --- | --- |
| Acquisition responses | `intraday/source-objects/responses`; shared observation payloads with separate attempt identities and original clocks |
| Market-response bodies | `intraday/source-objects/market-responses`; immutable bodies shared across dates, with separate receipt metadata |
| Minute history | Existing provider/symbol raw and normalized Parquet keys; nightly continuation imports saved daytime attempts before fetching its remaining tail |
| Minute availability | `intraday/source-objects/minute-receipts.sqlite3`; value/revision digests and original receipt references, with no second price table |
| Prepared model inputs | `intraday/system-1a/ready/snapshots`; shared exact objects or lossless chunks, referenced by preparation and model databases |

The adapters are supplied in `patches/intraday-shared-storage.patch` with exact
preimage, final-byte and dependency hashes in the adjacent JSON. Review those
against the complete local installation; public source can lack earlier local
adapters. A public package check is not evidence of installed integration. The
full composed source needs offline checks and a sealed generation; actual loaded
code is a separate observation. Preserve unrelated source and local science.
Roll out journal reference readers with `reference_writes=False` first. Enable
the compact default only after the current journal-reader roles have naturally
loaded that support; do not force a restart or assume another PC has adopted it.

Legacy inline responses, dated market paths and inline snapshots remain readable.
New bodies are immutable and content addressed. References reconstruct the exact
original bytes and snapshot identity. Different provider observations, revisions,
receipt clocks, order IDs and original outcomes are distinct evidence even when
some values match. Indexed history preserves actual receipt availability; old
unindexed history retains its explicit historical timing assumption.

The canonical-history bridge commits receipt evidence before its upsert and
advances each journal cursor only after persistence succeeds. Retrying repairs
interrupted work. Retained-data errors are reported per symbol and do not stop
ordinary provider continuation for other symbols. A conflicting legacy value with unknown receipt timing is
retained for ordinary provider overlap instead of being overwritten by an older
saved attempt. The existing datastore writer mutex protects this operation.

## Maintenance

`tools/atlas_datastore_manager.py` inventories and verifies allowlisted immutable
objects, then consolidates exact copies while preserving reader paths. Review
the saved plan and revalidate current ownership before applying bounded batches.
Apply requires exact-path quiescence evidence as well as review. Resume the
original plan after interruption; retain its staged originals and transaction
evidence. Busy or changed files remain pending; unique
data, mutable logs, model exports, databases and WAL/SHM files are excluded.

`tools/intraday_storage_migration.py` can replace exact historical inline
snapshots in supported preparation/model databases with shared references. It
requires verified reader bindings, preserves IDs and every unrelated table, and
uses short compare-and-swap transactions. Execution databases are excluded.
Freed SQLite pages are reusable capacity, not reclaimed filesystem space.

`tools/intraday_snapshot_compaction.py` converts explicitly selected raw snapshot
objects into lossless shared chunks. Apply requires actual compatible-reader
and current quiescence evidence. It independently reconstructs the original SHA
before removing a redundant raw representation. Its durable journal preserves
interrupted work and raced files. Include new chunk allocation when measuring
net savings; the first chunk seed can consume additional space.

Do not stop processes for maintenance, compact or replace an active database,
delete its WAL/SHM files, fabricate reader/quiescence evidence, or refit a model
when an original is missing. Offline database compaction requires separate
consumer review, capacity, integrity and full preservation checks.

The local scheduler checks sibling ownership before maintenance and between
short batches. Active or uncertain sibling work defers that wake. Process/file
ownership must also be checked: idle chats do not make an active database idle.
Recurring maintenance never gates independent data collection or an authorized
trading session. Report coverage, unresolved payload classes and measured savings
without claiming zero duplication from a partial scan or a dry run.
