from __future__ import annotations

import os
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path

import pytest
from filelock import Timeout

import datafetching.orchestrate as orchestration
import ml.prediction_runtime as prediction_runtime
from datafetching import schwab_fetch
from datafetching import FetchResult
from datafetching.parquet_store import ParquetStore
from datafetching.runtime_lock import exclusive_runtime_lock, runtime_lock_maintenance_gate
from options.publication import option_writer_lock_path


def test_loop_a_external_modes_never_enter_slow_failing_cme_or_options_lanes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[tuple[bool, bool, bool]] = []

    def provider_batch(
        symbols: tuple[str, ...],
        _store: ParquetStore,
        *,
        include_cme: bool,
        include_options: bool,
        include_schwab_price_history: bool,
        **_kwargs: object,
    ) -> dict[str, tuple[FetchResult, ...]]:
        observed.append(
            (include_cme, include_options, include_schwab_price_history)
        )
        if include_cme or include_options:
            time.sleep(0.5)
            raise TimeoutError("deliberately slow external provider")
        return {
            symbol: (
                FetchResult("databento", 0, 0, 0),
                FetchResult("schwab", 0, 0, 0),
            )
            for symbol in symbols
        }

    monkeypatch.setattr(orchestration, "run_symbols_fetch", provider_batch)
    started = time.perf_counter()
    failures = orchestration.run_cycle(
        ("GOOG", "NVDA"),
        ParquetStore(tmp_path),
        providers=("databento", "schwab"),
        requested_profile="continuation",
        include_cme=False,
        include_options=False,
        run_technical_calculations=False,
        run_fundamental_calculations=False,
        run_signal_calculations=False,
        datastore_target=None,
        datastore_path=tmp_path,
    )
    elapsed = time.perf_counter() - started

    assert failures == 0
    assert observed == [(False, False, False)]
    assert elapsed < 0.25


def test_writer_lock_rejects_double_writer_and_recovers_dead_owner(
    tmp_path: Path,
) -> None:
    lock = tmp_path / ".writer.lock"
    with exclusive_runtime_lock(lock, process_name="test writer"):
        with pytest.raises(RuntimeError, match="owns these artifacts"):
            with exclusive_runtime_lock(lock, process_name="second writer"):
                raise AssertionError("unreachable")
    assert not lock.exists()

    lock.write_text(
        "process=dead test writer\n"
        "pid=2147483647\n"
        "started_at=2020-01-01T00:00:00+00:00\n",
        encoding="utf-8",
    )
    with exclusive_runtime_lock(lock, process_name="restarted writer"):
        assert f"pid={os.getpid()}" in lock.read_text(encoding="utf-8")
    assert not lock.exists()


def test_runtime_lock_maintenance_gate_is_shared_and_reacquirable(
    tmp_path: Path,
) -> None:
    gate_path = tmp_path / ".ducketz-runtime-lock-maintenance.lock"

    with runtime_lock_maintenance_gate(tmp_path):
        assert gate_path.is_file()
        with pytest.raises(Timeout):
            with runtime_lock_maintenance_gate(tmp_path, timeout=0):
                raise AssertionError("unreachable")

    with runtime_lock_maintenance_gate(tmp_path, timeout=0):
        assert gate_path.is_file()


@pytest.mark.parametrize(
    ("lock_kind", "lock_context"),
    (
        pytest.param("loop_a", orchestration.orchestration_lock, id="loop-a"),
        pytest.param("loop_b", prediction_runtime.runtime_lock, id="loop-b"),
    ),
)
def test_loop_supervisor_lock_recovers_only_a_confirmed_dead_pid(
    tmp_path: Path,
    lock_kind: str,
    lock_context: Callable[[Path], AbstractContextManager[None]],
) -> None:
    lock = tmp_path / f".{lock_kind}.lock"
    stale_payload = (
        "process=dead test owner\n"
        "pid=2147483647\n"
        "started_at=2020-01-01T00:00:00+00:00\n"
        "token=stale\n"
    )
    lock.write_text(stale_payload, encoding="utf-8")

    with lock_context(lock):
        replacement = lock.read_text(encoding="utf-8")
        assert f"pid={os.getpid()}" in replacement
        assert "token=stale" not in replacement

    assert not lock.exists()


@pytest.mark.parametrize(
    ("lock_kind", "lock_context"),
    (
        pytest.param("loop_a", orchestration.orchestration_lock, id="loop-a"),
        pytest.param("loop_b", prediction_runtime.runtime_lock, id="loop-b"),
    ),
)
@pytest.mark.parametrize("owner_kind", ("live", "malformed", "zero"))
def test_loop_supervisor_lock_never_reclaims_live_or_unverifiable_owner(
    tmp_path: Path,
    lock_kind: str,
    lock_context: Callable[[Path], AbstractContextManager[None]],
    owner_kind: str,
) -> None:
    lock = tmp_path / f".{lock_kind}.lock"
    owner = {
        "live": str(os.getpid()),
        "malformed": "not-a-pid",
        "zero": "0",
    }[owner_kind]
    payload = (
        "process=existing test owner\n"
        f"pid={owner}\n"
        "started_at=2026-09-03T00:00:00+00:00\n"
        "token=existing\n"
    )
    lock.write_text(payload, encoding="utf-8")

    with pytest.raises(RuntimeError, match="Another Duckets"):
        with lock_context(lock):
            raise AssertionError("unreachable")

    assert lock.read_text(encoding="utf-8") == payload


@pytest.mark.parametrize("lock_kind", ("shared", "loop_a", "loop_b"))
def test_runtime_lock_cleanup_preserves_changed_owner(
    tmp_path: Path,
    lock_kind: str,
) -> None:
    lock = tmp_path / f".{lock_kind}.lock"
    if lock_kind == "shared":
        context = exclusive_runtime_lock(lock, process_name="test writer")
    elif lock_kind == "loop_a":
        context = orchestration.orchestration_lock(lock)
    else:
        context = prediction_runtime.runtime_lock(lock)

    with context:
        lock.write_text(
            "pid=2147483646\n"
            "started_at=2026-08-19T18:42:00Z\n"
            "token=replacement\n",
            encoding="utf-8",
        )

    assert "token=replacement" in lock.read_text(encoding="utf-8")

def test_inline_options_conflict_fails_before_slow_chain_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SlowSession:
        calls = 0

        def get_option_chain_snapshot(self, *_args: object, **_kwargs: object) -> object:
            self.calls += 1
            time.sleep(0.5)
            raise TimeoutError("deliberate slow option provider")

    class EmptyProvider:
        def fetch_quote(self, *_args: object, **_kwargs: object) -> object:
            raise RuntimeError("quote unavailable")

    session = SlowSession()
    monkeypatch.setattr(schwab_fetch, "_specs_for_profile", lambda _profile: ())
    with exclusive_runtime_lock(
        option_writer_lock_path(tmp_path),
        process_name="external Options runtime",
    ):
        started = time.perf_counter()
        result = schwab_fetch.fetch(
            "GOOG",
            ParquetStore(tmp_path),
            session=session,  # type: ignore[arg-type]
            provider=EmptyProvider(),  # type: ignore[arg-type]
            include_options=True,
        )
        elapsed = time.perf_counter() - started

    assert session.calls == 0
    assert result.error_files >= 1
    assert elapsed < 0.25


def test_loop_a_schwab_quote_only_mode_never_builds_price_history_specs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class QuoteFailureProvider:
        def fetch_quote(self, *_args: object, **_kwargs: object) -> object:
            raise RuntimeError("quote unavailable")

    monkeypatch.setattr(
        schwab_fetch,
        "_specs_for_profile",
        lambda _profile: pytest.fail("quote-only mode requested Schwab bars"),
    )

    result = schwab_fetch.fetch(
        "GOOG",
        ParquetStore(tmp_path),
        provider=QuoteFailureProvider(),  # type: ignore[arg-type]
        include_options=False,
        include_price_history=False,
    )

    assert result.provider == "schwab"
    assert result.error_files == 1
