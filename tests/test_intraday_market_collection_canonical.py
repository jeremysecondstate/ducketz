"""Canonical receipt/queue bytes retain native and provider normalization rules."""
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from types import MappingProxyType

import numpy as np
import pandas as pd
import pytest

from ml.intraday.market_collection import canonical


@pytest.mark.parametrize("value,expected", [
    (None, "null"),
    (True, "true"),
    (False, "false"),
    (0, "0"),
    (-123, "-123"),
    (2**80, "1208925819614629174706176"),
    (-0.0, "-0.0"),
    (1.25, "1.25"),
    (5e-324, "5e-324"),
    (float("nan"), "null"),
    (float("inf"), "null"),
    (float("-inf"), "null"),
    ("雪", '"雪"'),
    ('quote"\\\n', '"quote\\"\\\\\\n"'),
])
def test_native_scalar_canonical_bytes(value, expected):
    assert canonical(value) == expected.encode("utf-8")


def test_nested_queue_canonical_bytes_are_compact_sorted_utf8_and_nonmutating():
    value = {
        "tasks": {"task-b": {"attempts": 2, "snapshots": [], "due_at": "2026-10-21T20:00:00+00:00"},
                  "task-a": {"request": {"params": {"symbol": "雪", "raw": None}}, "window_finalized": False}},
        "schema_version": "intraday-market-collection-v1",
        "source_blocks": {}, "symbols": ["AAA", "BBB"],
    }
    expected = ('{"schema_version":"intraday-market-collection-v1","source_blocks":{},'
        '"symbols":["AAA","BBB"],"tasks":{"task-a":{"request":{"params":{"raw":null,"symbol":"雪"}},'
        '"window_finalized":false},"task-b":{"attempts":2,"due_at":"2026-10-21T20:00:00+00:00","snapshots":[]}}}')
    before = repr(value)
    assert canonical(value) == expected.encode("utf-8")
    assert repr(value) == before


@pytest.mark.parametrize("value,expected", [
    ({2: "two", 10: "ten"}, '{"10":"ten","2":"two"}'),
    ({1: "numeric", "1": "literal"}, '{"1":"literal"}'),
    ({"1": "literal", 1: "numeric"}, '{"1":"numeric"}'),
    ({True: "boolean", None: "none", 1.5: "float"}, '{"1.5":"float","None":"none","True":"boolean"}'),
])
def test_mapping_keys_keep_stringification_order_and_collision_rules(value, expected):
    assert canonical(value) == expected.encode("utf-8")


class PresentedDict(dict):
    def items(self):
        return [(2, "two"), (10, "ten")]


class PresentedList(list):
    def __iter__(self):
        return iter(("presented", None))


class MappingString(str, Mapping):
    def __iter__(self):
        return iter(("mapping",))

    def __len__(self):
        return 1

    def __getitem__(self, key):
        if key != "mapping":
            raise KeyError(key)
        return "view"


class NativeString(str):
    pass


class NativeInt(int):
    pass


class NativeFloat(float):
    pass


@pytest.mark.parametrize("value,expected", [
    (PresentedDict(hidden="must not replace presented items"), '{"10":"ten","2":"two"}'),
    (PresentedList(["hidden"]), '["presented",null]'),
    (MappingString("underlying scalar"), '{"mapping":"view"}'),
    (MappingProxyType({2: (None, True)}), '{"2":[null,true]}'),
    (NativeString("provider"), '"provider"'),
    (NativeInt(42), "42"),
    (NativeFloat(-0.0), "-0.0"),
    (NativeFloat("nan"), "null"),
])
def test_subclasses_and_mapping_views_keep_legacy_normalization(value, expected):
    assert canonical(value) == expected.encode("utf-8")


class ItemScalar:
    def item(self):
        return np.int64(7)


class ProviderSentinel:
    def __str__(self):
        return "provider-sentinel"


@pytest.mark.parametrize("value,expected", [
    (datetime(2026, 10, 21, 13, 0, 0, 123456, tzinfo=timezone(timedelta(hours=-7))),
     '"2026-10-21T20:00:00.123456+00:00"'),
    (datetime(2026, 10, 21, 13, 0, 0, 123456), '"2026-10-21T13:00:00.123456"'),
    (pd.Timestamp("2026-10-21T20:00:00.123456789Z"), '"2026-10-21T20:00:00.123456789+00:00"'),
    (pd.NaT, "null"),
    (pd.NA, "null"),
    (np.int64(42), "42"),
    (np.float32(1.25), "1.25"),
    (np.float64("nan"), "null"),
    (np.bool_(True), "true"),
    (ItemScalar(), "7"),
    (ProviderSentinel(), '"provider-sentinel"'),
    ((None, True, "tuple"), '[null,true,"tuple"]'),
])
def test_provider_scalars_clocks_and_missing_values_keep_receipt_bytes(value, expected):
    assert canonical(value) == expected.encode("utf-8")


def test_non_scalar_array_is_still_rejected():
    with pytest.raises(ValueError):
        canonical({"raw": np.asarray([1, 2])})


def test_unencodable_string_is_still_rejected():
    with pytest.raises(UnicodeEncodeError):
        canonical({"raw": "\ud800"})


def test_cyclic_container_is_still_rejected():
    value = []
    value.append(value)
    with pytest.raises(RecursionError):
        canonical(value)
