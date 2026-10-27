"""Portable delegation proof; pure stdlib, no accounts/providers/application imports.

Run from any reviewed package: python -B tests/check_intraday_starter_snapshot_validation.py
Optional --installed-root PATH also proves exact installed function/dependency bytes.
Explicit stubs test delegation composition; full indicator/science regressions stay separate.
"""
from __future__ import annotations
import argparse
import ast
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace


def sha(value):
    return hashlib.sha256(value).hexdigest()


def function(source, name):
    node = next(n for n in ast.parse(source).body
                if isinstance(n, ast.FunctionDef) and n.name == name)
    return "\n".join(source.splitlines()[node.lineno - 1:node.end_lineno]) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installed-root", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    package = root / "docs/development/patches"
    meta = json.loads((package / "intraday-starter-snapshot-validation.json").read_text(encoding="utf-8"))
    patch = (package / "intraday-starter-snapshot-validation.patch").read_bytes()
    passed = []

    def check(label, condition):
        if not condition:
            raise AssertionError(label)
        passed.append(label)

    check("metadata_schema", meta["schema"] == "intraday-starter-snapshot-validation-v1")
    check("single_owned_path", meta["operation"] == "modify" and meta["path"] == "ml/intraday/starter_sizing.py")
    check("patch_exact_bytes", sha(patch) == meta["patch_sha256"])
    diff = patch.decode().splitlines()
    check("single_file_patch", [x for x in diff if x.startswith("--- ")] == ["--- a/" + meta["path"]]
          and [x for x in diff if x.startswith("+++ ")] == ["+++ b/" + meta["path"]])
    check("single_patch_hunk", sum(x.startswith("@@") for x in diff) == 1)
    check("only_duplicate_guard_removed", [x[1:] for x in diff if x.startswith("-") and not x.startswith("---")] == [
        "    if digest(forecast_snapshot) != snapshot_hash:",
        '        raise ContractError("Original forecast snapshot hash changed")'])
    check("only_delegation_comment_added", [x[1:] for x in diff if x.startswith("+") and not x.startswith("+++")] == [
        "    # indicator_snapshot verifies this exact frozen snapshot before use."])
    old, new = meta["before_function"], meta["after_function"]
    check("before_function_bytes", sha(old.encode()) == meta["before_function_sha256"])
    check("after_function_bytes", sha(new.encode()) == meta["after_function_sha256"])
    hunk_lines = diff[next(i for i, line in enumerate(diff) if line.startswith("@@")) + 1:]
    old_hunk = "\n".join(line[1:] for line in hunk_lines if line.startswith((" ", "-"))) + "\n"
    new_hunk = "\n".join(line[1:] for line in hunk_lines if line.startswith((" ", "+"))) + "\n"
    check("patch_hunk_exact_function_reconstruction", old.count(old_hunk) == 1 and old.replace(old_hunk, new_hunk, 1) == new)
    before, after = ast.parse(old), ast.parse(new)
    before_function = before.body[0]
    guards = [n for n in before_function.body if isinstance(n, ast.If)]
    check("exact_one_original_guard", len(guards) == 1)
    guard = guards[0]
    check("original_guard_identity", ast.unparse(guard.test) == "digest(forecast_snapshot) != snapshot_hash"
          and ast.unparse(guard.body[0]) == "raise ContractError('Original forecast snapshot hash changed')")
    before_function.body.remove(guard)
    check("whole_function_ast_unchanged_except_guard", ast.dump(before, include_attributes=False) == ast.dump(after, include_attributes=False))
    strict = meta["strict_dependency"]
    strict_source = strict["source"]
    check("strict_function_bytes", sha(strict_source.encode()) == strict["function_sha256"])
    strict_ast = ast.parse(strict_source).body[0]
    digest_guard = next(n for n in strict_ast.body if isinstance(n, ast.If))
    check("public_strict_digest_still_required", ast.unparse(digest_guard.test) == "digest(forecast_snapshot) != forecast_snapshot_hash"
          and ast.unparse(digest_guard.body[0]) == "raise ContractError('Original forecast snapshot hash changed')")
    indicator_calls = [n for n in ast.walk(after) if isinstance(n, ast.Call)
                       and isinstance(n.func, ast.Name) and n.func.id == "indicator_snapshot"]
    check("exact_one_public_delegation", len(indicator_calls) == 1)
    call = indicator_calls[0]
    check("no_bypass_keyword", {x.arg for x in call.keywords} == {
        "symbol", "reference_at", "data_cutoff", "indicator_specs", "window_policy",
        "forecast_snapshot", "forecast_snapshot_hash"})
    check("same_snapshot_and_hash", ast.unparse(next(k.value for k in call.keywords if k.arg == "forecast_snapshot")) == "forecast_snapshot"
          and ast.unparse(next(k.value for k in call.keywords if k.arg == "forecast_snapshot_hash")) == "snapshot_hash")

    class ContractError(ValueError):
        pass

    class PrefixValidated(Exception):
        pass

    class Result:
        def __init__(self, value):
            self.value = value

        def payload(self):
            return deepcopy(self.value)

    class Spec:
        def __init__(self):
            self.name = "portable_stub"

        def validate(self):
            return self

    class Window:
        def validate(self):
            return self

    def portable_digest(value):
        # Explicit canonical fixture stub, not a replacement for production digest.
        return sha(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode())

    def instant(value):
        if not isinstance(value, str):
            raise ValueError("Explicit fixture clock required")
        result = datetime.fromisoformat(value)
        if result.tzinfo is None:
            raise ValueError("Fixture clock must be aware")
        return result.astimezone(timezone.utc)

    def identity(value):
        if not isinstance(value, str) or not value:
            raise ContractError("Explicit fixture identity required")
        return value

    def strict_prefix_config(*_):
        # Terminate only after unchanged actual public proof/clock/symbol checks.
        raise PrefixValidated()

    def environment():
        log = []

        def counted_digest(value):
            log.append("snapshot_digest")
            return portable_digest(value)

        strict_env = {"identity": identity, "instant": instant, "digest": counted_digest,
                      "ContractError": ContractError, "_indicator_config": strict_prefix_config}
        exec(compile(strict_source, "<unchanged-public-indicator>", "exec"), strict_env)

        def indicator(candles, **kwargs):
            log.append("public_indicator")
            if candles is not kwargs["forecast_snapshot"]["candles"]:
                raise AssertionError("Different candle object delegated")
            try:
                strict_env["indicator_snapshot"](candles, **kwargs)
            except PrefixValidated:
                log.append("strict_prefix_validated")
                return Result({"kind": "portable-indicator-stub", "snapshot_hash": kwargs["forecast_snapshot_hash"]})
            raise AssertionError("Strict proof prefix did not terminate")

        def reference(forecast, **kwargs):
            log.append("independent_reference")
            return {"symbol": forecast["symbol"], "stub": True}, None

        def evaluate(forecast, indicators, **kwargs):
            log.append("evaluate_size")
            return Result({"status": "stub-sized", "symbol": forecast["symbol"],
                           "probability": forecast["probability_not_down"], "reference": kwargs["symbol_reference"],
                           "indicators": indicators.payload()})

        env = {"starter_policy": lambda symbol: SimpleNamespace(symbol=symbol, indicators=(Spec(),), window=Window()),
               "digest": counted_digest, "ContractError": ContractError, "indicator_snapshot": indicator,
               "independent_reference": reference, "evaluate_size": evaluate,
               "_apply_direction_minimum": lambda p: p, "_encoded_result": lambda cls, p: cls(p),
               "VERSION": "portable-explicit-stub", "asdict": lambda v: deepcopy(v)}
        return env, log

    snapshot = {"candles": [{"opened_at": "2026-10-27T19:45:00+00:00", "close": 100}],
                "cutoff": "2026-10-27T20:00:31+00:00",
                "inference": {"symbol": "AAPL", "reference_at": "2026-10-27T19:45:00+00:00"},
                "nested_provenance": {"original": [1, 2, 3]}}
    base_forecast = {"symbol": "AAPL", "snapshot_hash": portable_digest(snapshot),
                     "reference_at": snapshot["inference"]["reference_at"], "training_cutoff": snapshot["cutoff"],
                     "probability_not_down": 0.5001}

    def execute(source, forecast, snap):
        env, log = environment()
        exec(compile("from __future__ import annotations\n" + source, "<portable-size-forecast>", "exec"), env)
        try:
            value = env["size_forecast"](forecast, forecast_snapshot=snap, account_equity=None,
                quote=None, calculated_at=forecast.get("training_cutoff"), maximum_quote_age_seconds=60)
            return ("return", value.payload()), log
        except Exception as exc:
            return ("error", type(exc).__name__, str(exc)), log

    for probability in (0.0, 0.4999, 0.5, 0.5001, 1.0):
        forecast = dict(base_forecast, probability_not_down=probability)
        expected, old_log = execute(old, forecast, deepcopy(snapshot))
        actual, new_log = execute(new, forecast, deepcopy(snapshot))
        check("explicit_stub_parity_" + str(probability), expected == actual and actual[0] == "return")
        check("one_digest_instead_of_two_" + str(probability), old_log.count("snapshot_digest") == 2 and new_log.count("snapshot_digest") == 1)
        check("proof_before_reference_" + str(probability), new_log.index("strict_prefix_validated") < new_log.index("independent_reference"))

    for field in ("candles", "cutoff", "inference", "nested_provenance"):
        corrupt = deepcopy(snapshot)
        if field == "candles":
            corrupt[field][0]["close"] = 101
        elif field == "cutoff":
            corrupt[field] = "2026-10-27T20:00:32+00:00"
        elif field == "inference":
            corrupt[field]["symbol"] = "MU"
        else:
            corrupt[field]["original"].append(4)
        a, log = execute(new, dict(base_forecast), corrupt)
        b, _ = execute(old, dict(base_forecast), corrupt)
        check("corrupt_rejected_" + field, a == b == ("error", "ContractError", "Original forecast snapshot hash changed"))
        check("corrupt_never_evaluated_" + field, "independent_reference" not in log and "evaluate_size" not in log)

    # Recheck every call; no successful prior proof can authorize later changed data.
    unchanged, first_log = execute(new, dict(base_forecast), deepcopy(snapshot))
    corrupt = deepcopy(snapshot)
    corrupt["nested_provenance"]["original"].append(99)
    changed, second_log = execute(new, dict(base_forecast), corrupt)
    check("no_verification_cache", unchanged[0] == "return" and changed[0:2] == ("error", "ContractError")
          and first_log.count("snapshot_digest") == second_log.count("snapshot_digest") == 1)
    malformed = dict(base_forecast)
    malformed.pop("reference_at")
    baseline_error, _ = execute(old, malformed, corrupt)
    candidate_error, _ = execute(new, malformed, corrupt)
    check("multiply_invalid_inputs_both_reject", baseline_error[0:2] == ("error", "ContractError")
          and candidate_error[0:2] == ("error", "KeyError"))

    installation = "not_requested"
    if args.installed_root is not None:
        actual = (args.installed_root / meta["path"]).read_text(encoding="utf-8")
        technical = (args.installed_root / strict["path"]).read_text(encoding="utf-8")
        check("installed_candidate_function", sha(function(actual, meta["function"]).encode()) == meta["after_function_sha256"])
        check("installed_strict_dependency_function", sha(function(technical, strict["function"]).encode()) == strict["function_sha256"])
        installation = "exact_candidate_and_strict_function_verified_only"
    print(json.dumps({"passed": len(passed), "checks": passed, "portable_scope": "Strict public delegation prefix and composition under explicit stubs; no indicator science",
                      "installed_function_check": installation, "network_calls": 0,
                      "application_imports": 0, "runtime_loading_or_live_recovery_proved": False}, indent=2))


if __name__ == "__main__":
    main()