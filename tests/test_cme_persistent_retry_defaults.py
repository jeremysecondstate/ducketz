from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from datafetching import cme_runtime as runtime


@pytest.mark.parametrize("options, expected", [({}, None), ({"retry_attempts": 2}, 2)])
def test_cycle_passes_default_or_explicit_limit_to_discovery_and_collection(
    tmp_path, monkeypatch, options, expected,
):
    limits = []
    spec = SimpleNamespace(schema="ohlcv-1m", group_key="context", dataset="GLBX.MDP3",
                           symbols=("ES.c.0",), key="fixture")
    provider = SimpleNamespace(specs=lambda: (spec,))

    def request(operation, **kwargs):
        limits.append(("discovery", kwargs["max_attempts"]))
        return operation()

    def collect(_store, **kwargs):
        limits.append(("collection", kwargs["retry_attempts"]))
        return SimpleNamespace(rows=0, partitions_written=0, partitions_reused=0)

    monkeypatch.setattr(runtime, "call_with_persistent_databento_retry", request)
    monkeypatch.setattr(runtime, "_collect_schema", collect)
    monkeypatch.setattr(runtime, "cme_archive_replay_pending", lambda *a, **k: False)
    monkeypatch.setattr(runtime, "materialize_cme_cross_asset_context", lambda *a, **k: None)
    result = runtime.run_cme_cycle(SimpleNamespace(root_dir=tmp_path), provider=provider,
                                   reporter=None, **options)
    assert result.schemas_succeeded == 1
    assert result.schemas_failed == 0
    assert limits == [("discovery", expected), ("collection", expected)]


@pytest.mark.parametrize("options", [
    {"retry_attempts": value} for value in (0, -1, True, False, 1.5, "2", float("inf"))
] + [
    {"retry_delay_seconds": value}
    for value in (0, -1, True, False, "4", None, float("nan"), float("inf"), 10 ** 400)
])
def test_invalid_cycle_retry_options_fail_before_provider_construction(monkeypatch, options):
    monkeypatch.setattr(runtime, "DatabentoCmeContextProvider",
                        lambda: pytest.fail("invalid retry options reached provider construction"))
    with pytest.raises(ValueError, match="CME retry_"):
        runtime.run_cme_cycle(object(), **options)


@pytest.mark.parametrize("arguments, expected", [([], None), (["--retry-attempts", "3"], 3)])
def test_cli_preserves_default_or_explicit_retry_limit(tmp_path, monkeypatch, arguments, expected):
    calls = []
    monkeypatch.setattr(runtime, "load_repository_environment", lambda: False)
    monkeypatch.setattr(runtime, "ParquetStore", lambda *a, **k: SimpleNamespace(root_dir=tmp_path))
    monkeypatch.setattr(runtime, "exclusive_runtime_lock", lambda *a, **k: nullcontext())
    monkeypatch.setattr(runtime, "_print_result", lambda _result: None)

    def cycle(_store, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(schemas_failed=0)

    monkeypatch.setattr(runtime, "run_cme_cycle", cycle)
    assert runtime.main(["--once", *arguments]) == 0
    assert len(calls) == 1
    assert calls[0]["retry_attempts"] == expected
    assert calls[0]["retry_delay_seconds"] == 4.0


@pytest.mark.parametrize("arguments", [
    ["--retry-attempts", "0"], ["--retry-attempts", "-1"], ["--retry-attempts", "1.5"],
    ["--retry-delay-seconds", "nan"], ["--retry-delay-seconds", "inf"],
    ["--retry-delay-seconds", "-1"],
    ["--retry-delay-seconds", "0"],
])
def test_invalid_cli_retry_options_fail_before_environment_loading(monkeypatch, arguments):
    monkeypatch.setattr(runtime, "load_repository_environment",
                        lambda: pytest.fail("invalid retry options loaded the environment"))
    with pytest.raises(SystemExit) as result:
        runtime.main(["--once", *arguments])
    assert result.value.code == 2
