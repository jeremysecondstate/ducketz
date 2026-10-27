# Frozen legacy source bodies; the oracle never calls the changed encoder.
from collections import OrderedDict, UserDict
from collections.abc import Mapping
from decimal import Decimal
from types import MappingProxyType
import hashlib
import json
import math
import sys
import pytest
from ml.intraday import scout_journal as journal, scout_protocol as protocol
MAX_INTEGER = 2**63 - 1
ProtocolError = protocol.ProtocolError
JournalCorruptionError = journal.JournalCorruptionError

def legacy_json_value(value):
    if value is None or type(value) in (str, bool):
        return value
    if type(value) is int and -MAX_INTEGER - 1 <= value <= MAX_INTEGER:
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise ProtocolError("Canonical JSON requires string object keys")
        return {key: legacy_json_value(item) for key, item in value.items()}
    if type(value) is list:
        return [legacy_json_value(item) for item in value]
    raise ProtocolError("Canonical JSON requires finite, bounded JSON values")


def legacy_canonical_sha256(payload: Mapping) -> str:
    """Hash sorted, compact UTF-8 JSON, not an authentication proof.

    Wire DTOs contain no floating-point fields. This helper also supports finite
    JSON values for surrounding evidence; it is not an RFC 8785 implementation.
    """
    if not isinstance(payload, Mapping):
        raise ProtocolError("Canonical payload must be an object")
    try:
        raw = json.dumps(legacy_json_value(payload), sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(raw) > 1024 * 1024:
            raise ProtocolError("Canonical payload exceeds one MiB")
        return hashlib.sha256(raw).hexdigest()
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        if isinstance(exc, ProtocolError):
            raise
        raise ProtocolError("Invalid canonical JSON") from exc


def legacy_encoded(value):
    legacy_canonical_sha256(value)  # Apply the protocol's bounded canonical JSON rules.
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False)


def legacy_checked_json(payload, digest):
    try:
        value = json.loads(payload)
        encoded = legacy_encoded(value)
        if encoded != payload or hashlib.sha256(encoded.encode("utf-8")).hexdigest() != digest:
            raise JournalCorruptionError("Stored payload hash or canonical bytes differ")
        return value
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise JournalCorruptionError("Invalid stored canonical payload") from exc



def chain(error):
    return (type(error), str(error), chain(error.__cause__) if error.__cause__ else None)


def outcome(call, value):
    try:
        return ("accepted", call(value))
    except Exception as error:
        return ("rejected", chain(error))


def tree(depth):
    value = {"x": 1}
    for _ in range(depth):
        value = {"x": value}
    return value


@pytest.mark.parametrize("value", [
    {}, {"unicode": "雪☃é", "newline": "a\nb"}, {"n": -(2**63)}, {"n": 2**63-1},
    {"n": -0.0}, {"n": 5e-324}, {"v": True}, {"v": None},
    {"list": [1, False, None, {"x": "ok"}]},
])
def test_plain_bytes_and_public_hash_match_frozen_legacy(value):
    expected = legacy_encoded(value)
    status = [True]
    assert protocol._canonical_bytes(value, _plain_mappings=status) == expected.encode("utf-8")
    assert status == [True]
    assert journal._encoded(value) == expected
    assert protocol.canonical_sha256(value) == legacy_canonical_sha256(value)


@pytest.mark.parametrize("value", [
    None, True, 1, "x", [], {1: "bad"}, {"x": float("nan")}, {"x": float("inf")},
    {"x": -float("inf")}, {"x": Decimal("1")}, {"x": object()}, {"x": (1, 2)},
    {"x": 2**63}, {"x": -(2**63)-1}, {"x": "\ud800"},
])
def test_rejections_and_exception_causes_match_frozen_legacy(value):
    assert outcome(journal._encoded, value) == outcome(legacy_encoded, value)
    assert outcome(protocol.canonical_sha256, value) == outcome(legacy_canonical_sha256, value)


@pytest.mark.parametrize("size", [1024*1024-1, 1024*1024, 1024*1024+1])
@pytest.mark.parametrize("unicode", [False, True])
def test_exact_utf8_boundary_matches_legacy(size, unicode):
    length = size - 8
    text = "雪" * (length // 3) + "a" * (length % 3) if unicode else "a" * length
    value = {"x": text}
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
    assert len(raw) == size
    assert outcome(journal._encoded, value) == outcome(legacy_encoded, value)
    assert outcome(protocol.canonical_sha256, value) == outcome(legacy_canonical_sha256, value)
    if size <= 1024*1024:
        assert protocol._canonical_bytes(value) == raw
    else:
        assert outcome(protocol._canonical_bytes, value) == outcome(legacy_encoded, value)


@pytest.mark.parametrize("kind", ["user", "proxy", "ordered", "dict-subclass"])
@pytest.mark.parametrize("placement", ["root", "nested", "list", "empty"])
def test_any_custom_mapping_retains_original_serialization(kind, placement):
    trace = []
    class DictSubclass(dict):
        def __iter__(self):
            trace.append("iter")
            return super().__iter__()
        def items(self):
            trace.append("items")
            return super().items()
    def factory():
        base = {} if placement == "empty" else {"x": 1}
        value = {"user": UserDict, "proxy": MappingProxyType,
                 "ordered": OrderedDict, "dict-subclass": DictSubclass}[kind](base)
        if placement == "nested":
            return {"v": value}
        if placement == "list":
            return {"v": [value]}
        return value
    old = outcome(legacy_encoded, factory())
    oldtrace = list(trace)
    trace.clear()
    assert outcome(journal._encoded, factory()) == old
    assert trace == oldtrace
    status = [True]
    protocol._canonical_bytes(factory(), _plain_mappings=status)
    assert status == [False]


@pytest.mark.parametrize("error_type", [None, TypeError, ValueError, RecursionError, RuntimeError])
def test_mutable_custom_override_access_order_and_errors_are_preserved(error_type):
    def factory(trace):
        class Dynamic(dict):
            def __iter__(self):
                trace.append("iter")
                return super().__iter__()
            def items(self):
                trace.append("items")
                if error_type is not None:
                    raise error_type("custom mapping failure")
                return [("x", trace.count("items"))]
        return {"nested": [Dynamic(x=0)]}
    oldtrace, newtrace = [], []
    assert outcome(journal._encoded, factory(newtrace)) == outcome(legacy_encoded, factory(oldtrace))
    assert newtrace == oldtrace


@pytest.mark.parametrize("base,value", [(int, 1), (float, 1.0), (str, "x"), (list, [1])])
def test_nonmapping_subclasses_keep_legacy_rejections(base, value):
    subclass = type("Subclass", (base,), {})
    payload = {"x": subclass(value)}
    assert outcome(journal._encoded, payload) == outcome(legacy_encoded, payload)
    assert outcome(protocol.canonical_sha256, payload) == outcome(legacy_canonical_sha256, payload)


def test_cycle_and_near_recursion_limit_keep_original_boundary():
    cycle = {}
    cycle["self"] = cycle
    assert outcome(journal._encoded, cycle) == outcome(legacy_encoded, cycle)
    previous = sys.getrecursionlimit()
    try:
        sys.setrecursionlimit(320)
        decisions = []
        for depth in range(100, 351):
            value = tree(depth)
            old = outcome(legacy_encoded, value)
            assert outcome(journal._encoded, value) == old
            assert outcome(protocol.canonical_sha256, value) == outcome(legacy_canonical_sha256, value)
            decisions.append(old[0])
        assert "accepted" in decisions and "rejected" in decisions
    finally:
        sys.setrecursionlimit(previous)


def test_plain_encoder_serializes_once_without_hash_and_hash_uses_same_bytes(monkeypatch):
    real_dump, real_hash = json.dumps, hashlib.sha256
    dumped, hashed = [], []
    def dump(value, **kwargs):
        result = real_dump(value, **kwargs)
        dumped.append(result)
        return result
    def digest(raw):
        hashed.append(raw)
        return real_hash(raw)
    monkeypatch.setattr(json, "dumps", dump)
    monkeypatch.setattr(hashlib, "sha256", digest)
    value = {"nested": [1, {"x": "雪"}]}
    expected = '{"nested":[1,{"x":"雪"}]}'
    assert journal._encoded(value) == expected
    assert dumped == [expected] and hashed == []
    assert protocol.canonical_sha256(value) == real_hash(expected.encode()).hexdigest()
    assert dumped == [expected, expected] and hashed == [expected.encode()]


def test_plain_mapping_tracker_uses_existing_normalization_walk(monkeypatch):
    real = protocol._json_value
    visits = []
    def visit(value, **kwargs):
        visits.append(value)
        return real(value, **kwargs)
    monkeypatch.setattr(protocol, "_json_value", visit)
    value = {"a": [1, {"b": "雪"}], "c": None}
    assert journal._encoded(value) == '{"a":[1,{"b":"雪"}],"c":null}'
    expected = [value, value["a"], 1, value["a"][1], "雪", None]
    assert [id(x) for x in visits] == [id(x) for x in expected]


@pytest.mark.parametrize("error_type", [TypeError, ValueError, UnicodeError, RecursionError, RuntimeError])
def test_public_hash_error_wrapping_stays_legacy_and_bytes_do_not_hash(monkeypatch, error_type):
    def fail(raw):
        raise error_type("hash seam failure")
    monkeypatch.setattr(hashlib, "sha256", fail)
    assert outcome(protocol.canonical_sha256, {"x": 1}) == outcome(legacy_canonical_sha256, {"x": 1})
    assert protocol._canonical_bytes({"x": 1}) == b'{"x":1}'
