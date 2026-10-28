"""Optional durable scalar proofs for semantically validated forecast originals.

Cache failures never decide an instruction or change scientific evidence. Exact
source identity/revision and immutable guarded cache records allow another local
process to reuse validated headers; every miss uses the original full validator.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import stat
import json
from pathlib import Path
import sqlite3
import time
import uuid

# Bump this contract when full semantic validation or header meanings change.
_CONTRACT = "validated-forecast-scalar-headers-v2"
_FIELDS = frozenset({"forecast_id", "pc", "symbol", "cycle_id", "reference_at", "issued_at",
    "mode", "ensemble_status", "target_open", "target_close", "population_contract", "source_dataset", "source_id", "reference_open"})
_INDEXED = ("forecast_id", "pc", "symbol", "cycle_id", "reference_at", "issued_at")
_SCHEMA = {
    "forecast_header_cache_meta": "CREATE TABLE forecast_header_cache_meta(singleton INTEGER PRIMARY KEY CHECK(singleton=1),epoch BLOB NOT NULL CHECK(typeof(epoch)='blob' AND length(epoch)=16))",
    "forecast_header_cache_rows": "CREATE TABLE forecast_header_cache_rows(cache_key TEXT PRIMARY KEY,payload BLOB NOT NULL CHECK(typeof(payload)='blob' AND length(payload)<=65536),record_mac BLOB NOT NULL CHECK(typeof(record_mac)='blob' AND length(record_mac)=32))",
    "forecast_header_cache_meta_insert": "CREATE TRIGGER forecast_header_cache_meta_insert BEFORE INSERT ON forecast_header_cache_meta WHEN EXISTS(SELECT 1 FROM forecast_header_cache_meta) BEGIN SELECT RAISE(ABORT,'Header cache identity cannot be replaced'); END",
    "forecast_header_cache_meta_update": "CREATE TRIGGER forecast_header_cache_meta_update BEFORE UPDATE ON forecast_header_cache_meta BEGIN SELECT RAISE(ABORT,'Header cache identity is immutable'); END",
    "forecast_header_cache_meta_delete": "CREATE TRIGGER forecast_header_cache_meta_delete BEFORE DELETE ON forecast_header_cache_meta BEGIN SELECT RAISE(ABORT,'Header cache identity is immutable'); END",
    "forecast_header_cache_rows_insert": "CREATE TRIGGER forecast_header_cache_rows_insert BEFORE INSERT ON forecast_header_cache_rows WHEN forecast_header_cache_write_allowed(NEW.cache_key,NEW.payload,NEW.record_mac)!=1 BEGIN SELECT RAISE(ABORT,'Header cache requires a validated original'); END",
    "forecast_header_cache_rows_update": "CREATE TRIGGER forecast_header_cache_rows_update BEFORE UPDATE ON forecast_header_cache_rows BEGIN SELECT RAISE(ABORT,'Header cache proofs are immutable'); END",
    "forecast_header_cache_rows_delete": "CREATE TRIGGER forecast_header_cache_rows_delete BEFORE DELETE ON forecast_header_cache_rows BEGIN SELECT RAISE(ABORT,'Header cache proofs are immutable'); END",
}


def _encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(raw):
    return hashlib.sha256(raw.encode()).hexdigest()


class DurableForecastHeaders:
    """A bounded, nonblocking cache with no source-database writes."""
    def __init__(self, source_path, *, budget_seconds=.25):
        source = Path(source_path)
        self.path = source.with_name(source.name + ".forecast-headers.sqlite3")
        self.key_path = self.path.with_name(self.path.name + ".key")
        self._secret = None
        try:
            self._remaining = min(5., max(0., float(budget_seconds)))
        except (ValueError, TypeError):
            self._remaining = 0.  # Invalid optional settings disable the cache.


    def _connect(self):
        db = sqlite3.connect(self.path, timeout=0)
        deadline = time.monotonic() + min(.05, max(0, self._remaining))
        db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        return db

    @staticmethod
    def _protect_secret(raw, *, decrypt=False):
        if os.name != "nt":
            return raw
        # Windows user-bound DPAPI; no broker credentials, UI or network.
        import ctypes
        from ctypes import wintypes
        class Blob(ctypes.Structure):
            _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]
        incoming = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
        source = Blob(len(raw), incoming)
        target = Blob()
        crypt = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        if decrypt:
            ok = crypt.CryptUnprotectData(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target))
        else:
            ok = crypt.CryptProtectData(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target))
        if not ok:
            raise OSError("Optional header-cache attestation protection unavailable")
        try:
            return ctypes.string_at(target.data, target.size)
        finally:
            kernel.LocalFree.argtypes = [ctypes.c_void_p]
            kernel.LocalFree.restype = ctypes.c_void_p
            kernel.LocalFree(target.data)

    def _key_material(self, *, create=False):
        if self._secret is not None:
            return self._secret
        if not self.key_path.exists():
            if not create:
                return None
            secret = secrets.token_bytes(32)
            protected = b"ducketz-header-cache-key-v1\0" + self._protect_secret(secret)
            if len(protected) > 65536:
                return None
            try:
                descriptor = os.open(self.key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                pass
            else:
                with os.fdopen(descriptor, "wb") as file:
                    file.write(protected)
        if os.name != "nt" and stat.S_IMODE(self.key_path.stat().st_mode) & 0o077:
            return None
        with self.key_path.open("rb") as file:
            raw = file.read(65537)
        prefix = b"ducketz-header-cache-key-v1\0"
        if len(raw) > 65536 or not raw.startswith(prefix):
            return None
        secret = self._protect_secret(raw[len(prefix):], decrypt=True)
        if len(secret) != 32:
            return None
        self._secret = secret
        return secret

    @staticmethod
    def _authenticate(secret, raw):
        return hmac.new(secret, raw if isinstance(raw, bytes) else raw.encode(), hashlib.sha256).digest()

    @staticmethod
    def _context(db):
        actual = dict(db.execute("SELECT name,sql FROM sqlite_master WHERE name LIKE 'forecast_header_cache_%'"))
        if actual != _SCHEMA:
            return None
        rows = db.execute("SELECT singleton,CASE WHEN typeof(epoch)='blob' AND length(epoch)=16 THEN epoch ELSE NULL END FROM forecast_header_cache_meta LIMIT 2").fetchall()
        if len(rows) != 1 or rows[0][0] != 1 or not isinstance(rows[0][1], bytes) or len(rows[0][1]) != 16:
            return None
        return (db.execute("PRAGMA schema_version").fetchone()[0], rows[0][1].hex())

    @staticmethod
    def _key(identity, signature, context):
        return _digest(_encoded([_CONTRACT, sorted(_FIELDS), identity, signature, context]))

    @staticmethod
    def _projection(value, signature):
        if (not isinstance(value, dict) or set(value) - _FIELDS
                or any(item is not None and not isinstance(item, str) for item in value.values())
                or any(value.get(field) != indexed for field, indexed in zip(_INDEXED, signature[:6]))):
            return None
        return dict(value)

    def load(self, identity, signature):
        return self.load_many(identity, [signature]).get(tuple(signature))

    def load_many(self, identity, signatures, *, diagnostics=None):
        """Read one inventory's authenticated headers with one cache snapshot."""
        db = None
        found = {}
        reason, sqlite_errorcode = "budget_disabled", None
        requested = min(2147483647, len(signatures)) if type(signatures) in {list, tuple} else 0
        expected_count = rejected = iterated = 0
        retained = False
        started = time.monotonic()
        if self._remaining <= 0:
            self._report_load(diagnostics, reason, requested, expected_count,
                              found, retained, rejected, sqlite_errorcode)
            return {}
        try:
            if not self.path.is_file():
                reason = "missing_file"
                return {}
            secret = self._key_material()
            if secret is None:
                reason = "missing_key"
                return {}
            db = self._connect()
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            context = self._context(db)
            if context is None:
                reason = "invalid_context"
                return {}
            expected = {}
            for signature in signatures:
                iterated = min(2147483647, iterated + 1)
                requested = iterated
                expected[self._key(identity, signature, context)] = tuple(signature)
            expected_count = len(expected)
            if not expected:
                reason = "empty"
                return {}
            reason = "complete"
            rows = db.execute("SELECT cache_key,"
                "CASE WHEN typeof(payload)='blob' AND length(payload)<=65536 THEN payload ELSE NULL END,"
                "CASE WHEN typeof(record_mac)='blob' AND length(record_mac)=32 THEN record_mac ELSE NULL END "
                "FROM forecast_header_cache_rows WHERE cache_key IN (SELECT value FROM json_each(?))",
                (_encoded(tuple(expected)),))
            for key, raw, mac in rows:
                if time.monotonic() - started >= self._remaining:
                    reason = "budget_exhausted"
                    break
                if (key not in expected or not isinstance(raw, bytes) or len(raw) > 65536
                        or not isinstance(mac, bytes) or len(mac) != 32
                        or not hmac.compare_digest(self._authenticate(secret, raw), mac)):
                    rejected = min(2147483647, rejected + 1)
                    continue
                record = json.loads(raw)
                signature = expected[key]
                if (not isinstance(record, dict) or set(record) != {'contract', 'identity', 'signature', 'context', 'projection'}
                        or record['contract'] != _CONTRACT or record['identity'] != list(identity)
                        or record['signature'] != list(signature) or record['context'] != list(context)):
                    rejected = min(2147483647, rejected + 1)
                    continue
                projection = self._projection(record['projection'], signature)
                if projection is not None:
                    found[signature] = projection
                else:
                    rejected = min(2147483647, rejected + 1)
            if reason == "complete" and rejected:
                reason = "invalid_record"
            retained = True
            return found
        except sqlite3.OperationalError as error:
            # An interrupted optional read cannot invalidate earlier records
            # already authenticated against this exact pinned cache context.
            # Unread or rejected records still use the full source validator.
            sqlite_errorcode = getattr(error, "sqlite_errorcode", None)
            if sqlite_errorcode == sqlite3.SQLITE_INTERRUPT:
                reason, retained = "sqlite_interrupt", True
                return found
            reason = "sqlite_error"
            return {}
        except sqlite3.Error as error:
            reason = "sqlite_error"
            sqlite_errorcode = getattr(error, "sqlite_errorcode", None)
            return {}
        except OSError:
            reason = "os_error"
            return {}
        except (ValueError, TypeError, AttributeError):
            reason = "invalid_record"
            rejected = min(2147483647, rejected + 1)
            return {}
        finally:
            if db is not None:
                try:
                    db.close()
                except (sqlite3.Error, OSError):
                    pass
            self._remaining -= time.monotonic() - started
            self._report_load(diagnostics, reason, requested, expected_count,
                              found, retained, rejected, sqlite_errorcode)

    @staticmethod
    def _report_load(output, reason, requested, expected, found, retained, rejected, code):
        """Only fixed scalar outcomes; diagnostics never affect cache results."""
        if type(output) is not dict:
            return
        try:
            validated = min(2147483647, len(found))
            output.clear()
            output.update(schema_version="forecast-cache-load-outcome-v1", reason=reason,
                requested_count=min(2147483647, requested),
                expected_count=min(2147483647, expected), validated_count=validated,
                returned_count=validated if retained else 0,
                discarded_count=0 if retained else validated, rejected_count=rejected,
                sqlite_errorcode=code if type(code) is int and 0 <= code <= 2147483647 else None)
        except Exception:
            pass

    def save(self, identity, signature, projection):
        """Called only with the result of the full source semantic validator."""
        self.save_many(identity, ((signature, projection),))

    def save_many(self, identity, entries):
        """Attest one validated inventory with one bounded cache transaction.

        The source reader supplies only fully validated scalar projections.
        Each inserted tuple still needs its exact authenticated whitelist entry;
        a cache failure or interrupted batch leaves ordinary source validation
        authoritative. The cache schema, contract and proofs stay unchanged.
        """
        db = None
        started = time.monotonic()
        if self._remaining <= 0:
            return
        try:
            entries = [(tuple(signature), checked) for signature, projection in entries
                       if (checked := self._projection(projection, signature)) is not None]
            if not entries:
                return
            secret = self._key_material(create=True)
            if secret is None:
                return
            existed = self.path.exists()
            db = self._connect()
            db.execute("BEGIN IMMEDIATE")
            if not existed:
                for sql in _SCHEMA.values():
                    db.execute(sql)
                db.execute("INSERT INTO forecast_header_cache_meta VALUES(1,?)", (uuid.uuid4().bytes,))
            context = self._context(db)
            if context is None:
                return
            expected = {}
            for signature, projection in entries:
                if time.monotonic() - started >= self._remaining:
                    return
                key = self._key(identity, signature, context)
                raw = _encoded(dict(contract=_CONTRACT, identity=list(identity), signature=list(signature), context=list(context), projection=projection)).encode()
                if len(raw) > 65536:
                    continue
                record = (raw, self._authenticate(secret, raw))
                if key in expected and expected[key] != record:
                    return
                expected[key] = record
            if not expected:
                return
            db.create_function("forecast_header_cache_write_allowed", 3,
                lambda cache_key, payload, record_mac: int(expected.get(cache_key) == (payload, record_mac)))
            db.executemany("INSERT OR IGNORE INTO forecast_header_cache_rows VALUES(?,?,?)",
                           ((key, *record) for key, record in expected.items()))
            db.commit()
        except (sqlite3.Error, OSError, ValueError, TypeError, AttributeError):
            pass
        finally:
            if db is not None:
                try:
                    db.close()
                except (sqlite3.Error, OSError):
                    pass
            self._remaining -= time.monotonic() - started
