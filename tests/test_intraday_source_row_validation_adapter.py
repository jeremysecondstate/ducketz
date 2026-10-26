"""Apply published hunks to authentic pre-change validator methods, offline."""
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import json
import math
from pathlib import Path
import re
import sys
from types import ModuleType
from typing import Mapping

import numpy as np
import pandas as pd
import pytest

from ml.intraday.prediction import ContractError, STEP, instant
from ml.market_gap_evidence import MARKERS, POLICY, assess_interval_gap, derive_gap_carry

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/intraday_source_row_validation"
PATCH = ROOT / "docs/development/patches/intraday-source-row-validation.patch"


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _patch_fragments(texts, patch):
    """Apply each exact unique context; reject partial or ambiguous patches."""
    lines = patch.splitlines(keepends=True)
    current, index = None, 0
    names = {"ml/intraday/coverage.py": "coverage-before.py.txt",
             "ml/intraday/recovered_population.py": "recovery-before.py.txt"}
    result = dict(texts)
    while index < len(lines):
        line = lines[index]
        if line.startswith("+++ b/"):
            current = names[line[len("+++ b/"):].strip()]
            index += 1
        elif line.startswith("@@"):
            assert current is not None
            old, new = [], []
            index += 1
            while index < len(lines) and not lines[index].startswith(("@@", "--- a/")):
                line = lines[index]
                assert line[0] in " +-", line
                if line[0] in " -": old.append(line[1:])
                if line[0] in " +": new.append(line[1:])
                index += 1
            old, new = "".join(old), "".join(new)
            assert old and result[current].count(old) == 1
            result[current] = result[current].replace(old, new, 1)
        else:
            assert line.startswith("--- a/"), line
            index += 1
    return result


def _module(name, filename, namespace):
    module = ModuleType(name)
    module.__file__ = str(filename)
    module.__package__ = name.rsplit(".", 1)[0]
    module.__dict__.update(namespace)
    sys.modules[name] = module
    return module


def _load(texts, suffix):
    package = "_source_row_validation_" + suffix
    root = ModuleType(package)
    root.__path__ = []
    sys.modules[package] = root
    common = dict(pd=pd, np=np, math=math, json=json, hashlib=hashlib,
                  Mapping=Mapping, ContractError=ContractError, STEP=STEP,
                  instant=instant, datetime=datetime, dataclass=dataclass)
    native = _module(package + ".data", FIXTURES / "native-support.py.txt", common)
    exec(compile(texts["native-support.py.txt"], native.__file__, "exec"), native.__dict__)
    coverage = _module(package + ".coverage", FIXTURES / "coverage-before.py.txt", common)
    exec(compile(texts["coverage-before.py.txt"], coverage.__file__, "exec"), coverage.__dict__)
    recovery = _module(package + ".recovery", FIXTURES / "recovery-before.py.txt",
        {**common, "verified_source_observation": coverage.verified_source_observation,
         "POLICY": POLICY, "MARKERS": MARKERS, "derive_gap_carry": derive_gap_carry})
    exec(compile(texts["recovery-before.py.txt"], recovery.__file__, "exec"), recovery.__dict__)
    inputs = _module(package + ".inputs", FIXTURES / "inputs.py.txt",
        {**common, "coverage": coverage, "timedelta": timedelta,
         "PRIMARY_CONTRACT": coverage.POPULATION_CONTRACT,
         "SessionBounds": native.SessionBounds, "assess_interval_gap": assess_interval_gap})
    exec(compile(texts["inputs.py.txt"], inputs.__file__, "exec"), inputs.__dict__)
    return coverage, recovery, inputs


@pytest.fixture(scope="module")
def versions():
    manifest = json.loads((FIXTURES / "manifest.json").read_bytes())
    assert _sha(PATCH.read_bytes()) == manifest["patch_sha256"]
    texts = {}
    for name, expected in manifest["fixture_sha256"].items():
        raw = (FIXTURES / name).read_bytes()
        assert _sha(raw) == expected
        texts[name] = raw.decode()
    patched = _patch_fragments(texts, PATCH.read_text(encoding="utf8"))
    for name, expected in manifest["patched_fragment_sha256"].items():
        assert _sha(patched[name].encode()) == expected
    return _load(texts, "before"), _load(patched, "after")


def _outcome(function, row):
    try:
        return "return", function(deepcopy(row))
    except Exception as exc:
        return "raise", type(exc).__name__, str(exc)


def test_patch_exactly_reconstructs_authenticated_methods_without_runtime_modules(versions):
    for version, suffix in zip(versions, ("before", "after")):
        coverage, recovery, _ = version
        assert coverage.verified_source_observation.__module__ == "_source_row_validation_" + suffix + ".coverage"
        assert recovery.verified_recovery_candle.__module__ == "_source_row_validation_" + suffix + ".recovery"
        assert Path(coverage.verified_source_observation.__code__.co_filename) == FIXTURES / "coverage-before.py.txt"
        assert Path(recovery.verified_recovery_candle.__code__.co_filename) == FIXTURES / "recovery-before.py.txt"


@pytest.mark.parametrize("indices", [tuple(range(15)), (0, 7, 14), (8,)])
@pytest.mark.parametrize("opened_at", ["2026-10-12T17:00:00Z", "1969-12-31T23:45:00Z",
                                        "2026-11-01T01:00:00-08:00"])
def test_dense_sparse_negative_epoch_and_offset_native_evidence_preserved(versions, indices, opened_at):
    before, after = versions
    row = before[2].source_candle(indices, opened_at=opened_at)
    original = deepcopy(row)
    assert _outcome(before[0].verified_source_observation, row) == ("return", True)
    assert _outcome(after[0].verified_source_observation, row) == ("return", True)
    assert row == original


@pytest.mark.parametrize("case", ["quarter_ns", "minute_ns", "wrong_first", "wrong_last",
    "native_naive", "native_nat", "native_none", "native_invalid", "available_naive", "available_nat",
    "available_early", "available_late", "coverage_early", "coverage_late", "duplicate", "unsorted",
    "missing", "wrong_source", "wrong_dataset", "wrong_aggregation", "invalid_json", "nan_price",
    "inf_volume", "bad_ohlc", "aggregate_changed", "synthetic", "native_marker", "boolean_count",
    "fractional_count", "empty_count", "empty_observations", "numpy_marker", "numpy_numeric"])
def test_source_guard_results_and_errors_remain_exact(versions, case):
    before, after = versions
    row = before[2].source_candle()
    evidence = row["evidence_json"]
    observations = evidence["observations"]
    if case == "quarter_ns": row["opened_at"] = pd.Timestamp(row["opened_at"]) + pd.Timedelta(1, "ns")
    elif case == "minute_ns": observations[4]["opened_at"] = pd.Timestamp(observations[4]["opened_at"]) + pd.Timedelta(1, "ns")
    elif case == "wrong_first": row["native_first_opened_at"] = observations[1]["opened_at"]
    elif case == "wrong_last": row["native_last_opened_at"] = observations[1]["opened_at"]
    elif case.startswith("native_") and case in {"native_naive", "native_nat", "native_none", "native_invalid"}:
        observations[0]["opened_at"] = {"native_naive": "2026-10-12T17:00:00", "native_nat": pd.NaT, "native_none": None, "native_invalid": "invalid"}[case]
    elif case == "available_naive": observations[0]["available_at"] = "2026-10-12T17:01:00"
    elif case == "available_nat": observations[0]["available_at"] = pd.NaT
    elif case == "available_early": observations[0]["available_at"] = row["opened_at"]
    elif case == "available_late": observations[0]["available_at"] = "2026-10-12T17:16:00Z"
    elif case == "coverage_early": evidence["source_coverage"]["closed_at"] = "2026-10-12T17:14:00Z"
    elif case == "coverage_late": evidence["source_coverage"]["opened_at"] = "2026-10-12T17:01:00Z"
    elif case == "duplicate": observations[1] = deepcopy(observations[0])
    elif case == "unsorted": observations.reverse()
    elif case == "missing": observations.pop()
    elif case == "wrong_source": observations[0]["source_id"] = "other-source"
    elif case == "wrong_dataset": evidence["source_dataset"] = "other-dataset"
    elif case == "wrong_aggregation": evidence["aggregation"] = "other-population"
    elif case == "invalid_json": row["evidence_json"] = "{invalid"
    elif case == "nan_price": row["open"] = float("nan")
    elif case == "inf_volume": row["volume"] = float("inf")
    elif case == "bad_ohlc": observations[0]["high"] = 1.0
    elif case == "aggregate_changed": row["volume"] += 1.0
    elif case == "synthetic": observations[0]["synthetic"] = True
    elif case == "native_marker": observations[0]["is_derived"] = True
    elif case == "boolean_count": row["observed_constituents"] = True
    elif case == "fractional_count": row["observed_constituents"] = 14.5
    elif case == "empty_count": row["source_empty_constituents"] = 1
    elif case == "empty_observations": observations.clear()
    elif case == "numpy_marker": observations[0]["synthetic"] = np.bool_(False)
    elif case == "numpy_numeric": row["observed_constituents"] = np.int64(15)
    old = _outcome(before[0].verified_source_observation, row)
    assert _outcome(after[0].verified_source_observation, row) == old
    if case not in {"numpy_numeric"}: assert old == ("return", False)


@pytest.mark.parametrize("indices", [tuple(range(15)), (0, 7, 14), (8,)])
def test_duration_and_clock_work_removed_with_full_proof_retained(versions, monkeypatch, indices):
    row = versions[0][2].source_candle(indices)
    counts = []
    for coverage, _, _ in versions:
        result = {"clock": 0, "duration": 0}
        original_utc = coverage._utc
        def clock(value):
            result["clock"] += 1
            return original_utc(value)
        class PandasProof:
            def __getattr__(self, name): return getattr(pd, name)
            def Timedelta(self, *args, **kwargs):
                result["duration"] += 1
                return pd.Timedelta(*args, **kwargs)
        with monkeypatch.context() as context:
            context.setattr(coverage, "_utc", clock)
            context.setattr(coverage, "pd", PandasProof())
            assert coverage.verified_source_observation(deepcopy(row))
        counts.append(result)
    assert counts == [{"clock": 6 + 4*len(indices), "duration": 2*len(indices)},
                      {"clock": 6 + 2*len(indices), "duration": 1}]


def _recovered(version, kind):
    _, recovery, inputs = version
    if kind == "primary": return recovery.wrap_primary(inputs.primary())
    if kind == "schwab": proof = inputs.assessment(schwab=[inputs.bar()])
    elif kind == "fmp": proof = inputs.assessment(fmp=[inputs.bar()])
    else: proof = inputs.assessment()
    return recovery.recovery_candle(proof, inputs.SESSION)


@pytest.mark.parametrize("kind", ["primary", "schwab", "fmp", "derived"])
def test_every_authentic_recovery_constructor_retains_canonical_expected_evidence(versions, kind):
    before, after = versions
    old, new = _recovered(before, kind), _recovered(after, kind)
    assert old is not None and old == new
    assert _outcome(before[1].verified_recovery_candle, old) == ("return", True)
    assert _outcome(after[1].verified_recovery_candle, old) == ("return", True)


@pytest.mark.parametrize("kind", ["primary", "schwab", "fmp", "derived"])
@pytest.mark.parametrize("case", ["price_changed", "bool_volume", "nan_volume", "marker_changed",
    "source_changed", "session_changed", "invalid_evidence", "evidence_mapping", "numpy_scalars",
    "timestamp_objects", "naive_clock", "nullable_clock", "extra_identity"])
def test_recovery_full_field_evidence_and_nullable_clock_results_preserved(versions, kind, case):
    before, after = versions
    row = _recovered(before, kind)
    if case == "price_changed": row["close"] += 1
    elif case == "bool_volume": row["volume"] = False
    elif case == "nan_volume": row["volume"] = float("nan")
    elif case == "marker_changed": row["synthetic"] = not row["synthetic"]
    elif case == "source_changed": row["source_id"] = "other"
    elif case == "session_changed": row["session_id"] = "other"
    elif case == "invalid_evidence": row["evidence_json"] = "{invalid"
    elif case == "evidence_mapping": row["evidence_json"] = json.loads(row["evidence_json"])
    elif case == "numpy_scalars": row.update(volume=np.float64(row["volume"]), synthetic=np.bool_(row["synthetic"]))
    elif case == "timestamp_objects":
        for field in ("opened_at", "available_at", "session_open", "session_close"): row[field] = pd.Timestamp(row[field])
    elif case == "naive_clock": row["opened_at"] = pd.Timestamp(row["opened_at"]).tz_localize(None)
    elif case == "nullable_clock":
        row.update(native_first_opened_at=pd.NaT, native_last_opened_at=float("nan"))
    elif case == "extra_identity": row["saved_snapshot_id"] = "offline-storage-only"
    old = _outcome(before[1].verified_recovery_candle, row)
    assert _outcome(after[1].verified_recovery_candle, row) == old
    if case in {"evidence_mapping", "numpy_scalars", "timestamp_objects", "extra_identity"} or case == "nullable_clock" and kind == "derived":
        assert old == ("return", True)
    else:
        assert old == ("return", False)


@pytest.mark.parametrize("kind", ["primary", "schwab", "fmp", "derived"])
def test_one_repeated_expected_encoding_removed_per_valid_recovery(versions, monkeypatch, kind):
    row = _recovered(versions[0], kind)
    counts = []
    for _, recovery, _ in versions:
        calls = []
        original = recovery._plain
        def plain(value):
            calls.append(value)
            return original(value)
        with monkeypatch.context() as context:
            context.setattr(recovery, "_plain", plain)
            assert recovery.verified_recovery_candle(deepcopy(row))
        counts.append(len(calls))
    assert counts[0] == counts[1] + 1
