"""Bounded verified candle routing for one independent scoring worker.

Warm polls inspect all small indexed identities in original availability order.
They are not full historical payload-corruption audits. Originals are validated
on first/change selection and reloaded immediately before an audit/outcome write.
No raw source absence proofs or large candle payloads remain in this cache.
"""
from __future__ import annotations

import json

from .prediction import ContractError, digest, encoded, instant


class ScoringCandleReader:
    MAX_ENTRIES = 4096
    MAX_BYTES = 16 * 1024 * 1024
    _INDEX = ('symbol', 'opened_at', 'available_at')
    _FIELDS = ('symbol', 'opened_at', 'available_at', 'close', 'synthetic',
               'availability_kind', 'source_dataset', 'source_id')

    def __init__(self, store, *, stopped=lambda: False):
        self.store, self.stopped = store, stopped
        self._cache = {}
        self._cache_bytes = 0

    def _discard(self, key):
        previous = self._cache.pop(key, None)
        if previous is not None:
            self._cache_bytes -= previous[2]

    def _read(self, identity):
        with self.store.connect() as db:
            row = db.execute('SELECT rowid,id,symbol,opened_at,available_at,payload '
                             'FROM candles WHERE id=?', (identity[1],)).fetchone()
        if row is None or tuple(row[:-1]) != tuple(identity):
            raise ContractError('Scoring candle indexed identity changed during selection')
        value = json.loads(row[-1])
        if (not isinstance(value, dict) or digest(value) != identity[1]
                or any(value.get(field) != indexed for field, indexed in zip(self._INDEX, identity[2:]))):
            raise ContractError('Persisted candle evidence hash or indexed identity changed')
        return value

    @classmethod
    def _projection(cls, identity, value):
        from .data import completed_15m_observation, synthetic_continuity_verified
        from .native_interval import CONTRACT, candle_matches_forecast
        routing = {key: value[key] for key in cls._FIELDS if key in value}
        if any(isinstance(item, (dict, list)) for item in routing.values()):
            raise ContractError('Scoring candle routing requires scalar original fields')
        return encoded(dict(identity=list(identity), candle_id=identity[1], candle=routing,
            actual=value.get('availability_kind') == 'actual',
            continuity_verified=synthetic_continuity_verified(value),
            completed=completed_15m_observation(value),
            matches_legacy=candle_matches_forecast(value, {}),
            matches_native=candle_matches_forecast(value, {'native_interval_contract': CONTRACT})))

    def __iter__(self):
        with self.store.connect() as db:
            rows = db.execute('SELECT rowid,id,symbol,opened_at,available_at '
                              'FROM candles ORDER BY available_at,rowid').fetchall()
        present = {row[1] for row in rows}
        for key in self._cache.keys() - present:
            self._discard(key)
        yield from self._selections(rows)

    def iter_targets(self, targets):
        """Route exact target instants before unrelated candle payload reads.

        This is a bounded selection, not a replacement for the ordinary
        historical inventory. Selected originals use the same validation and
        cache as full iteration; timestamps are compared as actual instants.
        """
        targets = {(symbol, instant(opened)) for symbol, opened in targets}
        if not targets or self.stopped():
            return
        symbols = encoded(sorted({symbol for symbol, _ in targets}))
        with self.store.connect() as db:
            rows = db.execute('SELECT rowid,id,symbol,opened_at,available_at '
                'FROM candles WHERE symbol IN (SELECT value FROM json_each(?)) '
                'ORDER BY available_at,rowid', (symbols,)).fetchall()
        selected = []
        for row in rows:
            if self.stopped():
                return
            if (row[2], instant(row[3])) in targets:
                selected.append(row)
        selected.sort(key=lambda row: (instant(row[4]), row[0]))
        yield from self._selections(selected)

    def _selections(self, rows):
        for row in rows:
            if self.stopped():
                return
            key = row[1]
            previous = self._cache.get(key)
            if previous is None or previous[0] != row:
                self._discard(key)
                original = self._read(row)
                projection = self._projection(row, original)
                del original
                size = len(projection.encode('utf-8'))
                if len(self._cache) < self.MAX_ENTRIES and self._cache_bytes + size <= self.MAX_BYTES:
                    self._cache[key] = (row, projection, size)
                    self._cache_bytes += size
            else:
                projection = previous[1]
            yield json.loads(projection)

    @staticmethod
    def matches(selection, forecast):
        """Apply original interval matching with cached original proof results."""
        import math
        from .native_interval import CONTRACT, FEATURE_VERSION
        def present(value):
            return value is not None and not (isinstance(value, float) and math.isnan(value))
        manifest = forecast.get('dataset_manifest') or {}
        tags = (forecast.get('native_interval_contract'), manifest.get('native_interval_contract'))
        if any(present(tag) and tag != CONTRACT for tag in tags):
            return False
        native = (forecast.get('feature_version') == FEATURE_VERSION or CONTRACT in tags)
        return selection['matches_native' if native else 'matches_legacy']

    def original(self, selection):
        """Reload/index/hash/provenance-check the exact original before a write."""
        original = self._read(selection['identity'])
        if self._projection(selection['identity'], original) != encoded(selection):
            raise ContractError('Scoring candle changed before outcome mutation')
        return original
