"""Durable receipt locator for immutable market snapshots, never their authority.

Model reads use SQLite mode=ro, point-seek exact nanosecond decision cutoffs,
then verify only the selected immutable JSON files. No provider calls or model
execution belong here. Legacy migration is explicit and bounded.
"""
from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path
import json
import sqlite3

from filelock import FileLock

INDEX_VERSION = "intraday-market-receipt-index-v1"
INDEX_NAME = "receipts.sqlite3"
MAX_CUTOFFS = 8192
MAX_SOURCES = 256
MAX_SELECTED = 1_000_000
LEGACY_MAX_FILES = 2048
LEGACY_MAX_BYTES = 64*1024*1024


class ReceiptIndexRequired(RuntimeError):
    pass


@contextmanager
def _connect(root, *, readonly=False):
    path = Path(root)/INDEX_NAME
    if readonly:
        connection = sqlite3.connect(path.resolve().as_uri()+"?mode=ro", uri=True, timeout=10)
        connection.execute("PRAGMA query_only=ON")
    else:
        connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        if readonly:
            connection.execute("BEGIN")
        with connection:
            yield connection
    finally:
        connection.close()


def initialize_receipt_index(root):
    """Initialize before the first snapshot; never infer legacy rows were indexed."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    with FileLock(str(root/"receipt-index-init.lock"), timeout=30):
        with _connect(root) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS receipts(
                    snapshot_id TEXT PRIMARY KEY, source_id TEXT NOT NULL,
                    symbol TEXT NOT NULL, source_kind TEXT NOT NULL,
                    receipt_ns INTEGER NOT NULL, available_ns INTEGER NOT NULL,
                    relative_path TEXT NOT NULL UNIQUE, sha256 TEXT NOT NULL,
                    status TEXT NOT NULL, header_json TEXT NOT NULL,
                    file_bytes INTEGER NOT NULL, modified_ns INTEGER NOT NULL,
                    CHECK(available_ns >= receipt_ns));
                CREATE INDEX IF NOT EXISTS receipt_seek
                    ON receipts(symbol,source_id,available_ns DESC,receipt_ns DESC);
                CREATE TABLE IF NOT EXISTS sources(
                    symbol TEXT NOT NULL, source_id TEXT NOT NULL,
                    PRIMARY KEY(symbol,source_id));
            """)
            existing = connection.execute("SELECT value FROM metadata WHERE key='version'").fetchone()
            if existing and existing[0] != INDEX_VERSION:
                raise ValueError("Unsupported receipt index version")
            if not existing:
                connection.executemany("INSERT INTO metadata(key,value) VALUES(?,?)", [
                    ("version", INDEX_VERSION),
                    ("legacy_complete", "0" if (root/"snapshots").exists() else "1")])


def _relative(root, path):
    root, path = Path(root).resolve(), Path(path).resolve()
    try:
        relative = path.relative_to(root)
    except ValueError:
        raise ValueError("Indexed receipt path escapes the collection root") from None
    if len(relative.parts) != 4 or relative.parts[0] != "snapshots" or path.suffix != ".json":
        raise ValueError("Unexpected immutable receipt path")
    return relative.as_posix()


def _entry(root, path, snapshot):
    from .market_collection import VERSION, instant, snapshot_digest, canonical
    if snapshot.get("schema_version") != VERSION or snapshot.get("snapshot_sha256") != snapshot_digest(snapshot):
        raise ValueError("Invalid immutable market snapshot")
    receipt, available = instant(snapshot["receipt_at"]).value, instant(snapshot["available_at"]).value
    if available < receipt:
        raise ValueError("Receipt availability precedes its actual receipt")
    header = {key:snapshot[key] for key in ("schema_version", "snapshot_id", "snapshot_sha256", "source_id",
        "symbol", "source_kind", "receipt_at", "available_at", "status")}
    header.update({key:snapshot.get(key) for key in ("provider", "dataset", "schema")})
    stat = Path(path).stat()
    return (snapshot["snapshot_id"], snapshot["source_id"], snapshot["symbol"], snapshot["source_kind"],
        receipt, available, _relative(root, path), snapshot["snapshot_sha256"], snapshot["status"],
        canonical(header).decode("utf-8"),stat.st_size,stat.st_mtime_ns)


def _insert(connection, entry):
    existing = connection.execute("SELECT * FROM receipts WHERE snapshot_id=?", (entry[0],)).fetchone()
    if existing is not None:
        if tuple(existing) != entry:
            raise ValueError("Immutable receipt index identity conflict")
        return False
    connection.execute("INSERT INTO receipts VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", entry)
    connection.execute("INSERT OR IGNORE INTO sources VALUES(?,?)", (entry[2], entry[1]))
    return True


def index_receipt(root, path, snapshot):
    """Commit after immutable file fsync and before queue success is committed."""
    root = Path(root)
    if not (root/INDEX_NAME).exists():
        initialize_receipt_index(root)
    entry = _entry(root, path, snapshot)
    with _connect(root) as connection:
        return _insert(connection, entry)


def _check(connection):
    metadata = dict(connection.execute("SELECT key,value FROM metadata"))
    if metadata.get("version") != INDEX_VERSION:
        raise ValueError("Unsupported receipt index version")
    return metadata.get("legacy_complete") == "1"


def _source_ids(value):
    if value is None:
        return None
    if isinstance(value, Mapping):
        value = [source for sources in value.values()
                 for source in ([sources] if isinstance(sources, str) else sources)]
    elif isinstance(value, str):
        value = [value]
    result = sorted(set(value))
    if len(result) > MAX_SOURCES or any(not isinstance(source, str) or not source for source in result):
        raise ValueError("Receipt source selection exceeds the explicit bound")
    return result


def _clocks(cutoff, since, decision_cutoffs):
    from .market_collection import instant
    upper, lower = instant(cutoff).value, instant(since).value
    if lower > upper:
        raise ValueError("Snapshot lower bound exceeds cutoff")
    if decision_cutoffs is None:
        return lower, upper, None
    decisions = sorted(set(instant(value).value for value in decision_cutoffs))
    if len(decisions) > MAX_CUTOFFS or any(value > upper or value < lower for value in decisions):
        raise ValueError("Decision receipt cutoffs exceed the requested range/bound")
    return lower, upper, decisions


def selected_receipt_entries(root, *, symbol, cutoff, since, decision_cutoffs=None, source_ids=None):
    """Return indexed metadata only; None means explicit legacy migration needed.

    Each point query uses (symbol,source,available_ns,receipt_ns), preserves all
    ties, and includes failed receipts. A failed latest receipt cannot expose an
    older success. Queries do not scan historical JSON or materialize its rows.
    """
    root = Path(root)
    lower, upper, decisions = _clocks(cutoff, since, decision_cutoffs)
    sources = _source_ids(source_ids)
    if not (root/INDEX_NAME).exists():
        return None
    with _connect(root, readonly=True) as connection:
        if not _check(connection):
            return None
        if sources is None:
            sources = [row[0] for row in connection.execute(
                "SELECT DISTINCT source_id FROM sources WHERE symbol IN (?,?) ORDER BY source_id LIMIT ?",
                (symbol.upper(), "*", MAX_SOURCES+1))]
            if len(sources) > MAX_SOURCES:
                raise ValueError("Explicit source selectors required for this receipt collection")
        selected = {}
        for source in sources:
            for scope in (symbol.upper(), "*"):
                if decisions is None:
                    cursor = connection.execute("""SELECT * FROM receipts INDEXED BY receipt_seek
                        WHERE symbol=? AND source_id=? AND available_ns BETWEEN ? AND ? AND receipt_ns>=?
                        ORDER BY available_ns,receipt_ns LIMIT ?""", (scope,source,lower,upper,lower,MAX_SELECTED+1))
                    for row in cursor:
                        selected[row["snapshot_id"]] = dict(row)
                        if len(selected) > MAX_SELECTED:
                            raise ValueError("Receipt selection requires bounded decision cutoffs")
                    continue
                # Batching cutoffs lowers Python/SQLite crossing overhead. Each
                # correlated subquery is an index seek, not a history scan.
                for offset in range(0, len(decisions), 128):
                    values = decisions[offset:offset+128]
                    if not values:
                        continue
                    sql = """WITH decisions(cutoff) AS (VALUES %s), chosen AS (
                        SELECT (SELECT snapshot_id FROM receipts INDEXED BY receipt_seek
                            WHERE symbol=? AND source_id=? AND available_ns BETWEEN ? AND decisions.cutoff AND receipt_ns>=?
                            ORDER BY available_ns DESC,receipt_ns DESC LIMIT 1) AS snapshot_id FROM decisions)
                        SELECT DISTINCT ties.* FROM chosen
                        JOIN receipts first ON first.snapshot_id=chosen.snapshot_id
                        JOIN receipts ties INDEXED BY receipt_seek ON ties.symbol=first.symbol
                            AND ties.source_id=first.source_id AND ties.available_ns=first.available_ns
                            AND ties.receipt_ns=first.receipt_ns""" % ",".join("(?)" for _ in values)
                    for row in connection.execute(sql, (*values,scope,source,lower,lower)):
                        selected[row["snapshot_id"]] = dict(row)
                        if len(selected) > MAX_SELECTED:
                            raise ValueError("Receipt decision/source selection exceeds its explicit bound")
    return tuple(sorted(selected.values(), key=lambda r: (r["available_ns"], r["source_id"], r["snapshot_id"])))


class SnapshotRef(Mapping):
    """Immutable index headers with explicit verified loading on a cache miss."""
    def __init__(self, root, entry):
        self._root, self._entry = Path(root), dict(entry)
        self._header = json.loads(entry["header_json"])
        self._path = (self._root/entry["relative_path"]).resolve()
        if _relative(self._root,self._path) != entry["relative_path"]:
            raise ValueError("Invalid indexed receipt path")
        from .market_collection import instant
        if (self._header.get("snapshot_sha256") != entry["sha256"]
                or instant(self._header["receipt_at"]).value != entry["receipt_ns"]
                or instant(self._header["available_at"]).value != entry["available_ns"]
                or any(self._header.get(key) != entry[key] for key in
                    ("snapshot_id", "source_id", "symbol", "source_kind", "status"))):
            raise ValueError("Receipt index header differs from its indexed identity")
        self._check_file()

    def _check_file(self):
        stat = self._path.stat()
        if stat.st_size != self._entry["file_bytes"] or stat.st_mtime_ns != self._entry["modified_ns"]:
            raise ValueError("Invalid immutable market snapshot: file changed after indexing")
        return self._path

    def __getitem__(self, key):
        return self._header[key]

    def __iter__(self):
        return iter(self._header)

    def __len__(self):
        return len(self._header)

    def load_verified(self):
        path = self._check_file()
        snapshot = json.loads(path.read_text(encoding="utf-8"))
        if _entry(self._root,path,snapshot) != tuple(self._entry[key] for key in
                ("snapshot_id", "source_id", "symbol", "source_kind", "receipt_ns", "available_ns",
                 "relative_path", "sha256", "status", "header_json", "file_bytes", "modified_ns")):
            raise ValueError("Immutable snapshot does not match its receipt index")
        # Do not retain raw records across thousands of historical references.
        # The caller may cache its derived vector, not this unbounded raw union.
        return snapshot

    def verify_unchanged(self):
        """Cheap cache-hit guard; full content is checked by load_verified()."""
        self._check_file()


def read_indexed_snapshots(root, entries, *, lazy=False):
    refs = tuple(SnapshotRef(root,entry) for entry in entries)
    return refs if lazy else tuple(ref.load_verified() for ref in refs)


def legacy_snapshots(root, *, symbol, cutoff, since, decision_cutoffs=None, source_ids=None,
                     max_files=LEGACY_MAX_FILES, max_bytes=LEGACY_MAX_BYTES):
    """Bounded read-only fallback for small legacy fixtures, never an annual scan."""
    from .market_collection import instant
    root = Path(root)
    lower, upper, decisions = _clocks(cutoff, since, decision_cutoffs)
    sources = _source_ids(source_ids)
    paths, total_bytes = [], 0
    for path in (root/"snapshots").glob("*/*/*.json"):
        paths.append(path)
        total_bytes += path.stat().st_size
        if len(paths) > max_files or total_bytes > max_bytes:
            raise ReceiptIndexRequired("Legacy market receipts need an explicit bounded receipt-index rebuild before model reads")
    entries = []
    for path in paths:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
        entry = _entry(root, path, snapshot)
        if (entry[2] in {symbol.upper(), "*"} and lower <= entry[4] <= entry[5] <= upper
                and (sources is None or entry[1] in sources)):
            entries.append((entry, snapshot))
    if decisions is not None:
        selected = set()
        groups = {}
        for entry, snapshot in entries:
            groups.setdefault((entry[2],entry[1]), []).append((entry,snapshot))
        for group in groups.values():
            for decision in decisions:
                eligible = [entry for entry,_ in group if entry[5] <= decision]
                if eligible:
                    latest = max((entry[5],entry[4]) for entry in eligible)
                    selected.update(entry[0] for entry in eligible if (entry[5],entry[4]) == latest)
        entries = [(entry,snapshot) for entry,snapshot in entries if entry[0] in selected]
    return tuple(snapshot for entry,snapshot in sorted(entries,key=lambda pair:(pair[0][5],pair[0][1],pair[0][0])))


def rebuild_receipt_index(root, *, max_files=100_000):
    """Explicit bounded local migration; never invoked automatically by readers.

    Snapshot files remain untouched. A bound/failure leaves legacy_complete=0,
    preserving honest incompleteness. Concurrent new appends index themselves.
    """
    root = Path(root)
    if type(max_files) is not int or max_files < 1:
        raise ValueError("A positive migration file bound is required")
    initialize_receipt_index(root)
    with FileLock(str(root/"receipt-index-rebuild.lock"), timeout=30):
        with _connect(root) as connection:
            connection.execute("UPDATE metadata SET value='0' WHERE key='legacy_complete'")
        paths = []
        for path in (root/"snapshots").glob("*/*/*.json"):
            paths.append(path)
            if len(paths) > max_files:
                raise ReceiptIndexRequired("Receipt migration exceeded its explicit file bound")
        added = 0
        for offset in range(0,len(paths),128):
            entries = [_entry(root,path,json.loads(path.read_text(encoding="utf-8"))) for path in paths[offset:offset+128]]
            with _connect(root) as connection:
                for entry in entries:
                    added += _insert(connection, entry)
        with _connect(root) as connection:
            connection.execute("UPDATE metadata SET value='1' WHERE key='legacy_complete'")
        return {"indexed_files":len(paths),"inserted_rows":added,"version":INDEX_VERSION}
