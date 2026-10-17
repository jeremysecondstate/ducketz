import importlib.util
import sys
from contextlib import contextmanager

import pytest

from ml.market_provenance import capture_runtime, preserve_bytes


@contextmanager
def loaded_fixture(tmp_path, text):
    source = tmp_path / "binding_fixture.py"
    source.write_text(text, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("binding_fixture", source)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        yield module, source
    finally:
        sys.modules.pop(spec.name, None)


def receipt(module):
    return capture_runtime([module.__name__])["modules"][module.__name__]


def test_loaded_code_evidence_survives_disk_replacement(tmp_path):
    source = tmp_path / "fixture_runtime.py"
    source.write_text("def price():\n    return 10\n", encoding="utf-8")
    spec = importlib.util.spec_from_file_location("fixture_runtime", source)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        first = capture_runtime([spec.name], object_root=tmp_path / "objects")
        assert first["modules"][spec.name]["loaded_functions_match_disk"]
        source.write_text("def price():\n    return 20\n", encoding="utf-8")
        second = capture_runtime([spec.name])
        before, after = first["modules"][spec.name], second["modules"][spec.name]
        assert module.price() == 10
        assert before["loaded_functions"] == after["loaded_functions"]
        assert before["disk_sha256_at_capture"] != after["disk_sha256_at_capture"]
        assert not after["loaded_functions_match_disk"]
    finally:
        sys.modules.pop(spec.name, None)


def test_immutable_objects_and_missing_modules_are_explicit(tmp_path):
    first = preserve_bytes(tmp_path, b"observed input")
    assert first == preserve_bytes(tmp_path, b"observed input")
    assert first != preserve_bytes(tmp_path, b"revised input")
    evidence = capture_runtime(["absent_market_module"])
    assert evidence["modules"]["absent_market_module"] == {"status": "not_loaded"}
    assert not evidence["historical_receipts_recovered"]


def test_two_dataclasses_retain_distinct_generated_binding_hashes(tmp_path):
    with loaded_fixture(tmp_path, '''from dataclasses import dataclass
@dataclass(frozen=True)
class First:
    price: float
    def value(self): return self.price
@dataclass(frozen=True)
class Second:
    volume: int
    def value(self): return self.volume
''') as (module, source):
        value = receipt(module)
        assert value["loaded_functions_match_disk"] is True
        assert {"First.__init__", "Second.__init__", "First.__repr__", "Second.__repr__"} <= set(value["loaded_functions"])
        assert {"First.__init__", "Second.__init__", "First.__repr__", "Second.__repr__"} <= set(value["uncomparable_functions"])
        assert value["source_comparable_functions"] == ["First.value", "Second.value"]
        assert not value["unmatched_functions"]


def test_property_variants_and_decorated_source_are_compared_separately(tmp_path):
    source_text = '''from functools import wraps
def decorate(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs): return fn(*args, **kwargs)
    return wrapper
@decorate
def price(): return 10
class Quote:
    @property
    def value(self): return 10
    @value.setter
    def value(self, amount): self.amount = amount
    @value.deleter
    def value(self): del self.amount
'''
    with loaded_fixture(tmp_path, source_text) as (module, source):
        value = receipt(module)
        assert value["loaded_functions_match_disk"] is True
        assert {"price", "price.__wrapped__", "Quote.value.fget", "Quote.value.fset", "Quote.value.fdel"} <= set(value["source_comparable_functions"])
        source.write_text(source_text.replace("self.amount = amount", "self.amount = amount + 1"), encoding="utf-8")
        changed = receipt(module)
        assert changed["loaded_functions_match_disk"] is False
        assert changed["unmatched_functions"] == ["Quote.value.fset"]


def test_external_decorator_is_not_asserted_to_match_source(tmp_path):
    with loaded_fixture(tmp_path, '''from contextlib import contextmanager
@contextmanager
def prices():
    yield 10
''') as (module, source):
        value = receipt(module)
        assert value["loaded_functions_match_disk"] is True
        assert "prices" in value["loaded_functions"] and "prices" in value["uncomparable_functions"]
        assert "prices.__wrapped__" in value["source_comparable_functions"]


@pytest.mark.parametrize("binding", ["price", "Quote.price"])
def test_foreign_function_monkey_patch_remains_a_mismatch(tmp_path, binding):
    with loaded_fixture(tmp_path, '''def price(): return 10
class Quote:
    def price(self): return 10
''') as (module, source):
        def foreign(*args): return 99
        if binding == "price": module.price = foreign
        else: module.Quote.price = foreign
        value = receipt(module)
        assert value["loaded_functions_match_disk"] is False
        assert value["unmatched_functions"] == [binding]
        assert binding in value["loaded_functions"] and binding not in value["uncomparable_functions"]


def test_deleted_source_function_is_not_reclassified_as_generated(tmp_path):
    with loaded_fixture(tmp_path, "def price(): return 10\n") as (module, source):
        source.write_text("def renamed(): return 10\n", encoding="utf-8")
        value = receipt(module)
        assert value["loaded_functions_match_disk"] is False
        assert value["unmatched_functions"] == ["price"]


def test_only_generated_functions_has_no_source_match_claim(tmp_path):
    with loaded_fixture(tmp_path, '''from dataclasses import dataclass
@dataclass
class Quote:
    price: float
''') as (module, source):
        value = receipt(module)
        assert value["loaded_functions"]
        assert value["loaded_functions_match_disk"] is None
        assert not value["source_comparable_functions"]


def test_runtime_class_wrapper_retains_hash_and_compares_original_source(tmp_path):
    source_text = '''from sklearn.base import BaseEstimator, TransformerMixin
class ContextTransform(TransformerMixin, BaseEstimator):
    def fit(self, X, y=None): return self
    def transform(self, X): return X
'''
    with loaded_fixture(tmp_path, source_text) as (module, source):
        value = receipt(module)
        assert value["loaded_functions_match_disk"] is True
        assert "ContextTransform.transform" in value["loaded_functions"]
        assert "ContextTransform.transform" in value["uncomparable_functions"]
        assert "ContextTransform.transform.__wrapped__" in value["source_comparable_functions"]
        source.write_text(source_text.replace("return X", "return X.copy()"), encoding="utf-8")
        changed = receipt(module)
        assert changed["loaded_functions_match_disk"] is False
        assert changed["unmatched_functions"] == ["ContextTransform.transform.__wrapped__"]
        assert changed["loaded_functions"] == value["loaded_functions"]


def test_structural_hash_ignores_interning_and_reference_graph_changes():
    from ml.market_provenance import _code_hash, _code_structure, CODE_HASH_ALGORITHM
    def price(): return 10
    text = "".join(("runtime_", "constant_", "uniquely_named_", "test"))
    shared = tuple([text, 1.5, b"native", (2, None)])
    copied = tuple([text, 1.5, b"native", (2, None)])
    before = price.__code__.replace(co_consts=(None, shared, shared))
    equivalent = price.__code__.replace(co_consts=(None, shared, copied))
    first = _code_hash(before)
    assert first == _code_hash(equivalent)
    sys.intern(text)
    assert first == _code_hash(before)
    changed = before.replace(co_consts=(None, shared, (*copied[:-1], (3, None))))
    assert first != _code_hash(changed)
    assert _code_structure(True) != _code_structure(1) != _code_structure(1.0)
    assert _code_structure(0.0) != _code_structure(-0.0)
    assert _code_structure((1,2)) != _code_structure(frozenset((1,2)))
    assert _code_structure(frozenset((1,'2',b'3'))) == _code_structure(frozenset((b'3','2',1)))
    assert _code_structure(complex(1,-2)) == ['complex',float(1).hex(),float(-2).hex()]
    assert _code_structure(Ellipsis) == ['ellipsis']
    assert CODE_HASH_ALGORITHM == 'python-code-structure-json-v1'


def test_runtime_receipt_versions_structural_hash_explicitly(tmp_path):
    from ml.market_provenance import CODE_HASH_ALGORITHM
    with loaded_fixture(tmp_path, "def price(): return 10\n") as (module, source):
        value = capture_runtime([module.__name__])
        assert value['schema_version'] == 'market-input-runtime-provenance-v3'
        assert value['modules'][module.__name__]['loaded_function_hash_algorithm'] == CODE_HASH_ALGORITHM
