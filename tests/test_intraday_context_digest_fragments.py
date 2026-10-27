"""Fixed-field reuse preserves canonical cache integrity for every decoded hit."""
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
import zlib

import pytest

from ml.intraday import market_context as context_module
from ml.intraday import market_context_cache as cache_module
from ml.intraday.market_context import (
    MarketContext, _context_snapshot, _json, _owned_context_digest, _selectors,
    market_context_contract,
)
from ml.intraday.market_context_cache import ContextCache, _ContextDigestFragments
from tests.test_intraday_market_context import CUTOFF, sealed


def example(version, selectors):
    contract = market_context_contract(selectors, version=version)
    names = tuple(contract["feature_names"])
    value = MarketContext("DÖCU\\\"", "2026-10-16T14:15:00+00:00", names,
        tuple((-0.0, 1.25, 1e-200, 1e200)[i % 4] for i in range(len(names))),
        ({"text": "é\\\"\n雪", "nested": [{"x": -0.0}, None, True]},), "untrusted",
        tuple(sorted(_selectors(selectors).items())), version)
    return contract, value


@pytest.mark.parametrize("version", ["intraday-causal-market-context-v1",
    "intraday-causal-market-context-v2", "intraday-causal-market-context-v3"])
@pytest.mark.parametrize("selectors", [{}, {"stock_quote": ["quote:é", "quote:雪"]},
    {"stock_quote": ["quote"], "fundamentals": ["facts"], "cross_asset": ["future"]}])
def test_fixed_fragments_match_original_canonical_snapshot_and_fresh_mutations(version, selectors):
    contract, value = example(version, selectors)
    fragments = _ContextDigestFragments.from_contract(contract, _selectors(selectors))
    expected = hashlib.sha256(_json(_context_snapshot(value, list(value.source_evidence))).encode()).hexdigest()
    assert fragments.digest(value) == expected == _owned_context_digest(value)
    # A saved digest is never accepted as proof of the decoded values/evidence.
    mutated = replace(value, values=(0.0, *value.values[1:]))
    assert fragments.digest(mutated) == _owned_context_digest(mutated) != expected
    nested = deepcopy(value.source_evidence)
    nested[0]["nested"][0]["x"] = 2
    mutated = replace(value, source_evidence=nested)
    assert fragments.digest(mutated) == _owned_context_digest(mutated) != expected
    # Static contract state is detached from subsequent caller changes.
    contract["feature_names"][0] = "edited"
    assert fragments.digest(value) == expected


@pytest.mark.parametrize("field", ["cutoff", "symbol", "feature_names", "source_ids", "feature_version"])
def test_non_native_or_changed_fixed_fields_preserve_original_serializer(field):
    contract, value = example("intraday-causal-market-context-v3", {"stock_quote": ["quote"]})
    fragments = _ContextDigestFragments.from_contract(contract, _selectors(contract["source_ids"]))
    calls = []
    class EqualText(str):
        def __eq__(self, other):
            calls.append(other)
            return True
        __hash__ = str.__hash__
    fields = {"cutoff": EqualText("different"), "symbol": EqualText("different"),
        "feature_names": (EqualText("different"), *value.feature_names[1:]),
        "source_ids": ((EqualText("different"), ("quote",)),),
        "feature_version": EqualText("different")}
    changed = replace(value, **{field: fields[field]})
    expected = _owned_context_digest(changed)
    expected_calls = calls[:]
    calls.clear()
    assert fragments.digest(changed) == expected
    assert calls == expected_calls


@pytest.mark.parametrize("where", ["owned", "snapshot", "hash", "json", "dumps", "loads", "policy"])
def test_native_helper_overrides_keep_original_digest_path(monkeypatch, where):
    contract, value = example("intraday-causal-market-context-v3", {})
    fragments = _ContextDigestFragments.from_contract(contract, {})
    if where == "owned":
        monkeypatch.setattr(cache_module, "_owned_context_digest", lambda _: "custom-owned")
        assert fragments.digest(value) == "custom-owned"
        return
    if where == "snapshot":
        monkeypatch.setattr(context_module, "_context_snapshot", lambda *args: {"custom": "snapshot"})
    elif where == "hash":
        monkeypatch.setattr(context_module, "_hash", lambda _: "custom-hash")
    elif where == "json":
        original = context_module._json
        monkeypatch.setattr(context_module, "_json", lambda obj: original({"custom": obj}))
    elif where == "dumps":
        original = json.dumps
        monkeypatch.setattr(json, "dumps", lambda obj, **kwargs: original({"custom": obj}, **kwargs))
    elif where == "loads":
        original = json.loads
        monkeypatch.setattr(json, "loads", lambda *args, **kwargs: original(*args, **kwargs))
    else:
        monkeypatch.setattr(context_module, "STATE_POLICY", "changed-policy")
    assert fragments.digest(value) == _owned_context_digest(value)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), object(), "\ud800"])
def test_dynamic_serialization_errors_match_original(bad):
    contract, value = example("intraday-causal-market-context-v3", {})
    fragments = _ContextDigestFragments.from_contract(contract, {})
    changed = replace(value, source_evidence=({"bad": bad},))
    with pytest.raises(Exception) as original:
        _owned_context_digest(changed)
    with pytest.raises(type(original.value)) as actual:
        fragments.digest(changed)
    assert str(actual.value) == str(original.value)


@pytest.mark.parametrize("field", ["values", "source_evidence"])
def test_reencoded_cache_payload_cannot_hide_corrupt_dynamic_values(tmp_path, field):
    snapshot = sealed()
    path = tmp_path / "cache.sqlite3"
    with ContextCache(path) as seed:
        expected = seed.build([snapshot], symbol="DOCU", cutoff=CUTOFF)
    import sqlite3
    with sqlite3.connect(path) as db:
        key, payload = db.execute("SELECT cache_key,payload FROM contexts").fetchone()
        envelope = json.loads(zlib.decompress(payload))
        if field == "values":
            envelope["context"]["values"][0] += 1
        else:
            envelope["context"]["source_evidence"][0]["tampered"] = "after decoding"
        payload = zlib.compress(_json(envelope).encode())
        db.execute("UPDATE contexts SET payload=?,payload_sha256=? WHERE cache_key=?",
            (payload, hashlib.sha256(payload).hexdigest(), key))
    with ContextCache(path) as actual:
        assert actual.build([snapshot], symbol="DOCU", cutoff=CUTOFF) == expected
        assert (actual.invalid_entries, actual.hits, actual.misses) == (1, 0, 1)


def test_caller_contract_mutation_still_fails_before_reusing_fixed_fields(tmp_path):
    from ml.intraday.prediction import ContractError
    contract = market_context_contract({})
    with ContextCache(tmp_path / "cache.sqlite3") as cache:
        cache.build([], symbol="DOCU", cutoff=CUTOFF, contract=contract)
        contract["feature_names"][0] = "changed-after-first-hit"
        with pytest.raises(ContractError, match="changed during cache transaction"):
            cache.build([], symbol="DOCU", cutoff=CUTOFF, contract=contract)

@pytest.mark.parametrize("where", ["cache_json", "context_json", "cache_hash", "dumps", "loads", "cache_class", "context_class"])
def test_non_native_serializer_skips_fragment_construction_without_callbacks(monkeypatch, where):
    contract = market_context_contract({})
    calls = []
    def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise RuntimeError("custom serializer must not be newly invoked")
    if where == "cache_json":
        monkeypatch.setattr(cache_module, "_json", forbidden)
    elif where == "context_json":
        monkeypatch.setattr(context_module, "_json", forbidden)
    elif where == "cache_hash":
        monkeypatch.setattr(cache_module, "_hash", forbidden)
    elif where in {"dumps", "loads"}:
        monkeypatch.setattr(json, where, forbidden)
    else:
        class ContextSubclass(MarketContext):
            pass
        monkeypatch.setattr(cache_module if where == "cache_class" else context_module,
                            "MarketContext", ContextSubclass)
    assert _ContextDigestFragments.from_contract(contract, {}) is None
    assert calls == []

@pytest.mark.parametrize("part", ["JSONEncoder", "__init__", "encode", "iterencode", "default",
    "encode_basestring", "encode_basestring_ascii", "c_make_encoder", "_make_iterencode"])
def test_custom_encoder_preserves_original_callback_shape_and_skips_new_fragments(monkeypatch, part):
    contract, value = example("intraday-causal-market-context-v3", {})
    fragments = _ContextDigestFragments.from_contract(contract, {})
    calls = []
    if part == "JSONEncoder":
        class Encoder(json.JSONEncoder):
            def encode(self, obj):
                calls.append(type(obj))
                return super().encode(obj)
        monkeypatch.setattr(json, "JSONEncoder", Encoder)
    else:
        owner = json.JSONEncoder if part in {"__init__", "encode", "iterencode", "default"} else json.encoder
        original = getattr(owner, part)
        def changed(*args, **kwargs):
            calls.append(tuple(type(arg) for arg in args))
            return original(*args, **kwargs)
        monkeypatch.setattr(owner, part, changed)
    expected = _owned_context_digest(value)
    original_calls = calls[:]
    calls.clear()
    assert fragments.digest(value) == expected
    assert calls == original_calls
    calls.clear()
    assert _ContextDigestFragments.from_contract(contract, {}) is None
    assert calls == []

@pytest.mark.parametrize("field", ["contract", "selectors", "feature_names", "version", "selector_ids"])
def test_custom_contract_containers_do_not_receive_new_preparation_callbacks(field):
    contract = market_context_contract({"stock_quote": ["quote"]})
    selectors = _selectors(contract["source_ids"])
    calls = []
    class CustomDict(dict):
        def __getitem__(self, key):
            calls.append("getitem")
            return super().__getitem__(key)
        def items(self):
            calls.append("items")
            return super().items()
    class CustomList(list):
        def __iter__(self):
            calls.append("iterate")
            return super().__iter__()
    class CustomTuple(tuple):
        def __iter__(self):
            calls.append("iterate")
            return super().__iter__()
    class CustomText(str):
        def __eq__(self, other):
            calls.append("equal")
            return super().__eq__(other)
    if field == "contract":
        contract = CustomDict(contract)
    elif field == "selectors":
        selectors = CustomDict(selectors)
    elif field == "feature_names":
        contract["feature_names"] = CustomList(contract["feature_names"])
    elif field == "version":
        contract["version"] = CustomText(contract["version"])
    else:
        selectors["stock_quote"] = CustomTuple(selectors["stock_quote"])
    assert _ContextDigestFragments.from_contract(contract, selectors) is None
    assert calls == []


def test_custom_context_attribute_callbacks_keep_original_order():
    contract, value = example("intraday-causal-market-context-v3", {})
    fragments = _ContextDigestFragments.from_contract(contract, {})
    calls = []
    class ContextSubclass(MarketContext):
        def __getattribute__(self, name):
            calls.append(name)
            return super().__getattribute__(name)
    custom = ContextSubclass(**vars(value))
    calls.clear()
    expected = _owned_context_digest(custom)
    expected_calls = calls[:]
    calls.clear()
    assert fragments.digest(custom) == expected
    assert calls == expected_calls


@pytest.mark.parametrize("where", ["cache", "context"])
def test_late_context_class_replacement_uses_original_digest(monkeypatch, where):
    contract, value = example("intraday-causal-market-context-v3", {})
    fragments = _ContextDigestFragments.from_contract(contract, {})
    class ContextSubclass(MarketContext):
        pass
    monkeypatch.setattr(cache_module if where == "cache" else context_module,
                        "MarketContext", ContextSubclass)
    assert fragments.digest(value) == _owned_context_digest(value)

@pytest.mark.parametrize("constant", ["FEATURE_VERSION", "STATE_POLICY", "FEATURE_POLICY"])
def test_custom_policy_constants_skip_new_preparation_callbacks(monkeypatch, constant):
    contract, value = example("intraday-causal-market-context-v3", {})
    fragments = _ContextDigestFragments.from_contract(contract, {})
    calls = []
    class CustomText(str):
        def __eq__(self, other):
            calls.append(other)
            return super().__eq__(other)
    monkeypatch.setattr(context_module, constant, CustomText(getattr(context_module, constant)))
    assert _ContextDigestFragments.from_contract(contract, {}) is None
    assert calls == []
    expected = _owned_context_digest(value)
    expected_calls = calls[:]
    calls.clear()
    assert fragments.digest(value) == expected
    assert calls == expected_calls
