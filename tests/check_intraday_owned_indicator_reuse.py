"""Exact extracted indicator validation, ownership and sizing parity; offline."""
from __future__ import annotations
import ast
from copy import deepcopy
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN, ROUND_HALF_UP, localcontext
import hashlib
import inspect
import json
import math
from numbers import Real
from pathlib import Path
import socket
import sys
from types import ModuleType
from typing import Mapping, Sequence
import unittest
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
META = json.loads((ROOT / "docs/development/patches/intraday-owned-indicator-reuse.json").read_text(encoding="utf8"))
PATCH = ROOT / "docs/development/patches/intraday-owned-indicator-reuse.patch"
NETWORK = []


def source(record):
    text = record["source"]
    if hashlib.sha256(text.encode()).hexdigest() != record["sha256"]:
        raise AssertionError("Exact source extract changed")
    ast.parse(text)
    return text


def install(ns, record):
    exec(compile("from __future__ import annotations\n" + source(record), "exact-source", "exec"), ns)


def namespaces(phase):
    module = ModuleType("_owned_indicator_" + phase)
    sys.modules[module.__name__] = module
    ns = module.__dict__
    ns.update({name: globals()[name] for name in (
        "asdict", "dataclass", "replace", "datetime", "timedelta", "timezone", "Decimal", "InvalidOperation",
        "ROUND_HALF_EVEN", "ROUND_HALF_UP", "localcontext", "hashlib", "json", "math", "Real", "Mapping", "Sequence", "np", "pd")})
    for path in ("ml/intraday/prediction.py", "ml/stock_direction_policy.py", "ml/intraday/sizing.py", "ml/intraday/technical_sizing.py"):
        for record in META["unchanged_dependencies"][path]["extracts"].values():
            install(ns, record)
    for record in META["sources"]["ml/intraday/technical_sizing.py"]["extracts_" + phase].values():
        install(ns, record)
    starter = ModuleType("_owned_starter_" + phase)
    sys.modules[starter.__name__] = starter
    ss = starter.__dict__
    ss.update(ns)
    ss["__name__"] = starter.__name__
    for record in META["unchanged_dependencies"]["ml/intraday/starter_sizing.py"]["extracts"].values():
        install(ss, record)
    for record in META["sources"]["ml/intraday/starter_sizing.py"]["extracts_" + phase].values():
        install(ss, record)
    return ns, ss


BEFORE, START_BEFORE = namespaces("before")
AFTER, START_AFTER = namespaces("after")


def fixture(ns, ss, probability=.72):
    ref = datetime(2026, 10, 12, 17, tzinfo=timezone.utc)
    cutoff = ref + timedelta(minutes=15, seconds=3)
    stamp = lambda t: t.isoformat()
    snapshot = {"cutoff": stamp(cutoff), "inference": {"symbol": "AAPL", "reference_at": stamp(ref)},
                "candles": [{"fixture_only": True, "close": 100.}]}
    f = dict(symbol="AAPL", reference_at=stamp(ref), reference_open="100", training_cutoff=stamp(cutoff),
             issued_at=stamp(cutoff), data_through=stamp(ref + ns["STEP"]), target_open=stamp(ref + 2 * ns["STEP"]),
             target_close=stamp(ref + 3 * ns["STEP"]), target_version=ns["TARGET_VERSION"],
             probability_not_down=probability, source_id="fixture:observed", source_dataset="FIXTURE",
             snapshot_hash=ns["digest"](snapshot))
    f["forecast_id"] = ns["digest"](f)
    policy = ss["starter_policy"]("AAPL")
    config = ns["_indicator_config"](policy.indicators, policy.window)
    value = dict(version=ns["VERSION"], symbol="AAPL", reference_at=stamp(ref), data_cutoff=stamp(cutoff),
                 forecast_snapshot_hash=f["snapshot_hash"], indicator_config=config, indicator_config_hash=ns["digest"](config),
                 status="ready", reason=None, reference_open="100", source_id="fixture:observed", source_dataset="FIXTURE",
                 features={"ema20.distance": .02, "sma20.distance": .01, "rsi14.value": 65., "bb20.percent_b": .7,
                           "stoch14.k": .8, "vwap20.distance": .02, "momentum4.value": .03, "atr14.relative": .01,
                           "relative_volume20.value": 1.2}, candles=[{"fixture_only": True, "evidence": {"nested": [1, 2, 3]}}])
    indicator = ns["_encoded_result"](ns["IndicatorSnapshot"], value)
    equity = ns["AvailableInput"](100000., stamp(cutoff), "fixture-equity", {"id": "equity-fixture"})
    quote = ss["QuoteObservation"](99., 101., stamp(cutoff), stamp(cutoff), "fixture-quote", {"id": "quote-fixture"})
    return [f, snapshot, indicator, policy, equity, quote]


def starter(ns, ss, bundle, **changes):
    f, frozen, indicator, _, equity, quote = bundle
    def factory(candles, **kw):
        assert candles is frozen["candles"] and kw["forecast_snapshot"] is frozen
        assert kw["forecast_snapshot_hash"] == f["snapshot_hash"]
        return indicator
    prior = ss.get("indicator_snapshot")
    ss["indicator_snapshot"] = factory
    try:
        arguments = dict(forecast_snapshot=frozen, account_equity=equity, quote=quote,
                         calculated_at=f["issued_at"], maximum_quote_age_seconds=60.)
        arguments.update(changes)
        return ss["size_forecast"](f, **arguments)
    finally:
        if prior is None:
            ss.pop("indicator_snapshot", None)
        else:
            ss["indicator_snapshot"] = prior


def outcome(fn, *args, **kw):
    try:
        result = fn(*args, **kw)
        return "return", result.result_id, result.payload_json
    except Exception as exc:
        return "exception", type(exc).__name__, str(exc)


class IndicatorReuseParity(unittest.TestCase):
    def test_exact_package_hashes(self):
        self.assertEqual(hashlib.sha256(PATCH.read_bytes()).hexdigest(), META["patch_sha256"])
        self.assertEqual([line[6:] for line in PATCH.read_text().splitlines() if line.startswith("+++ b/")], list(META["sources"]))
        for record in META["sources"].values():
            for phase in ("before", "after"):
                for extract in record["extracts_" + phase].values():
                    source(extract)
        for record in META["unchanged_dependencies"].values():
            for extract in record["extracts"].values():
                source(extract)

    def test_public_signatures(self):
        self.assertEqual(inspect.signature(BEFORE["evaluate_size"]), inspect.signature(AFTER["evaluate_size"]))
        self.assertEqual(inspect.signature(START_BEFORE["size_forecast"]), inspect.signature(START_AFTER["size_forecast"]))

    def test_helper_body_preserves_every_validation(self):
        record = META["sources"]["ml/intraday/technical_sizing.py"]
        old = ast.parse(source(record["extracts_before"]["evaluate_size"])).body[0]
        new = ast.parse(source(record["extracts_after"]["_evaluate_size_with_snapshot"])).body[0]
        new.name = old.name
        old.body, new.body = old.body[1:], new.body[1:]
        returns = [node for node in ast.walk(new) if isinstance(node, ast.Return) and isinstance(node.value, ast.Tuple)]
        self.assertEqual(len(returns), 1)
        self.assertEqual(ast.dump(returns[0].value.elts[1]), ast.dump(ast.Name(id="snapshot", ctx=ast.Load())))
        returns[0].value = returns[0].value.elts[0]
        self.assertEqual(ast.dump(old), ast.dump(new))

    def test_existing_readiness_two_call_renames_only(self):
        record = META["sources"]["ml/intraday/readiness.py"]
        old, new = {}, {}
        exec(source(record["extracts_before"]["SOURCE_INTERFACES"]), old)
        exec(source(record["extracts_after"]["SOURCE_INTERFACES"]), new)
        expected = deepcopy(old["SOURCE_INTERFACES"])
        expected["independent_starter_sizing"]["ml/intraday/starter_sizing.py"]["size_forecast"] = {"indicator_snapshot", "_evaluate_size_with_snapshot"}
        expected["live_account_adapter"]["ml/intraday/account_execution.py"]["IntradayAccountExecutor.step"] = {"size_forecast", "_submission_capacity", "_submit"}
        self.assertEqual(expected, new["SOURCE_INTERFACES"])

    def test_exactly_one_owned_indicator_decode(self):
        counts, results = {}, []
        for phase, ns, ss in (("before", BEFORE, START_BEFORE), ("after", AFTER, START_AFTER)):
            original = ns["IndicatorSnapshot"].payload
            counts[phase] = 0
            def payload(obj, phase=phase, original=original):
                counts[phase] += 1
                return original(obj)
            ns["IndicatorSnapshot"].payload = payload
            try:
                results.append(starter(ns, ss, fixture(ns, ss)))
            finally:
                ns["IndicatorSnapshot"].payload = original
        self.assertEqual(counts, {"before": 2, "after": 1})
        self.assertEqual(results[0].payload_json, results[1].payload_json)

    def test_private_owned_tree_and_final_result_are_isolated(self):
        bundle = fixture(AFTER, START_AFTER)
        f, frozen, indicator, policy, equity, quote = bundle
        _, owned = AFTER["_evaluate_size_with_snapshot"](f, indicator, symbol_policy=policy, calculated_at=f["issued_at"])
        original = indicator.payload()
        owned["candles"][0]["evidence"]["nested"][0] = -1
        owned["features"]["ema20.distance"] = -1
        self.assertEqual(indicator.payload(), original)
        final = starter(AFTER, START_AFTER, bundle)
        exact = final.payload_json
        decoded = final.payload()
        decoded["indicator_snapshot"]["candles"][0]["evidence"]["nested"][0] = -2
        frozen["candles"][0]["close"] = -3
        equity.evidence["id"] = quote.evidence["id"] = "mutated"
        self.assertEqual(final.payload_json, exact)
        self.assertEqual(final.payload()["reference_context"]["quote"]["evidence"]["id"], "quote-fixture")

    def test_invalid_forecast_precedes_external_snapshot(self):
        class Spy:
            snapshot_id = "spy"
            calls = 0
            def payload(self):
                self.calls += 1
                raise RuntimeError("external payload must not run")
        results = []
        for ns, ss in ((BEFORE, START_BEFORE), (AFTER, START_AFTER)):
            f = fixture(ns, ss)[0]
            f["forecast_id"] = "invalid"
            spy = Spy()
            results.append(outcome(ns["evaluate_size"], f, spy, calculated_at=f["issued_at"]))
            self.assertEqual(spy.calls, 0)
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[0][0], "exception")

    def test_network_unused(self):
        self.assertEqual(NETWORK, [])


def add_case(name, change):
    def test(self):
        results = []
        for ns, ss in ((BEFORE, START_BEFORE), (AFTER, START_AFTER)):
            bundle = fixture(ns, ss)
            arguments = change(ns, ss, bundle) or {}
            results.append(outcome(starter, ns, ss, bundle, **arguments))
        self.assertEqual(results[0], results[1])
    setattr(IndicatorReuseParity, "test_case_" + name, test)


for index, probability in enumerate((0., .2, .4999, .49990000001, .5, .50009999999, .5001, .72, 1.)):
    def change(ns, ss, bundle, probability=probability):
        bundle[:] = fixture(ns, ss, probability)
    add_case("probability_" + str(index), change)
for name, arguments in (("missing_quote", {"quote": None}), ("missing_equity", {"account_equity": None}),
                        ("age_zero", {"maximum_quote_age_seconds": 0}), ("age_bool", {"maximum_quote_age_seconds": True}),
                        ("age_none", {"maximum_quote_age_seconds": None}), ("age_nan", {"maximum_quote_age_seconds": float("nan")})):
    add_case(name, lambda ns, ss, bundle, arguments=arguments: arguments)
for field, value in (("symbol", "IONQ"), ("snapshot_hash", "bad"), ("source_id", "other"),
                     ("target_version", "other"), ("reference_open", "999"), ("target_close", "2026-10-12T18:00:00+00:00")):
    def change(ns, ss, bundle, field=field, value=value):
        bundle[0][field] = value
        bundle[0].pop("forecast_id")
        bundle[0]["forecast_id"] = ns["digest"](bundle[0])
    add_case("forecast_" + field, change)
for field, value in (("symbol", "IONQ"), ("source_id", "other"), ("forecast_snapshot_hash", "bad"),
                     ("reference_open", "999"), ("data_cutoff", "2026-10-12T17:15:04+00:00")):
    def change(ns, ss, bundle, field=field, value=value):
        decoded = bundle[2].payload()
        decoded[field] = value
        bundle[2] = ns["_encoded_result"](ns["IndicatorSnapshot"], decoded)
    add_case("indicator_" + field, change)
for name, transform in (("hash", lambda item: replace(item, snapshot_id="0" * 64)),
                        ("nested", lambda item: replace(item, payload_json=item.payload_json.replace("[1,2,3]", "[1,2,4]"))),
                        ("json", lambda item: replace(item, payload_json="{"))):
    def change(ns, ss, bundle, transform=transform):
        bundle[2] = transform(bundle[2])
    add_case("external_" + name, change)


def run():
    saved = {name: getattr(socket, name) for name in ("create_connection", "getaddrinfo")}
    connect, connect_ex = socket.socket.connect, socket.socket.connect_ex
    def deny(*args, **kwargs):
        NETWORK.append("blocked")
        raise AssertionError("Portable indicator checks prohibit network calls")
    try:
        socket.create_connection = socket.getaddrinfo = deny
        socket.socket.connect = socket.socket.connect_ex = deny
        result = unittest.TextTestRunner(verbosity=1).run(unittest.defaultTestLoader.loadTestsFromTestCase(IndicatorReuseParity))
    finally:
        for name, value in saved.items():
            setattr(socket, name, value)
        socket.socket.connect, socket.socket.connect_ex = connect, connect_ex
    print(json.dumps(dict(checks=result.testsRun, failures=len(result.failures), errors=len(result.errors), network_attempts=len(NETWORK),
                          limits="Exact source evaluation and wrapper ownership with an isolated fixture indicator factory; actual full TA/native proof composition is separate.")))
    return 0 if result.wasSuccessful() and not NETWORK else 1


if __name__ == "__main__":
    raise SystemExit(run())
