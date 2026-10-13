from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
import sqlite3

import pytest

from ml.intraday import scout_runtime_gate as gate
from ml.intraday.prediction import ContractError, TARGET_VERSION, digest, encoded, instant
from ml.intraday.scout_protocol import Grant, Request
from ml.intraday.store import ShadowStore


def make_case(tmp_path, *, reference="2026-10-08T17:00:00Z", pc="Scout", mode="live",
              probability=.72, quote_age=1):
    ref = instant(reference)
    at = ref + timedelta(minutes=16, seconds=10)
    store = ShadowStore(tmp_path / "forecast.sqlite3")
    started = ref + timedelta(seconds=1)
    session_id = "manual-scout-15m-test"
    store.begin_cycle("cycle", dict(pc=pc, mode=mode, experiment=session_id,
        started_at=(ref + timedelta(minutes=15)).isoformat(), reference_at=ref.isoformat(),
        symbols=["DOCU"], configuration_hash="c" * 64))
    snapshot_hash, _ = store.freeze_inputs("cycle", "DOCU", {"input": "immutable"})
    forecast = store.save_forecast(dict(pc=pc, symbol="DOCU", cycle_id="cycle",
        reference_at=ref.isoformat(), reference_open="100",
        target_open=(ref + timedelta(minutes=30)).isoformat(),
        target_close=(ref + timedelta(minutes=45)).isoformat(),
        issued_at=(ref + timedelta(minutes=16, seconds=2)).isoformat(),
        information_available_at=(ref + timedelta(minutes=15, seconds=3)).isoformat(),
        data_through=(ref + timedelta(minutes=15)).isoformat(), target_version=TARGET_VERSION,
        snapshot_hash=snapshot_hash, mode=mode, availability_basis="observed",
        probability_not_down=probability, configured_count=2, successful_count=2,
        ensemble_status="full", models=[dict(name=name, status="success",
            probability_not_down=probability) for name in ("a", "b")]))
    request = Request("request", "a" * 64, session_id, "DOCU", "BUY" if probability >= .5 else "SELL",
        4, "100", forecast["forecast_id"], forecast["forecast_id"], snapshot_hash, "b" * 64,
        ref.isoformat(), (ref + timedelta(minutes=16, seconds=3)).isoformat(),
        forecast["target_close"], forecast["target_close"])
    grant = Grant("grant", request.request_id, request.sha256, request.account_fingerprint,
        session_id, 1, 2, "reservation", (ref + timedelta(minutes=16, seconds=4)).isoformat(),
        forecast["target_close"])
    state = SimpleNamespace(now=at, stop=False, manual=True, proof=True)
    kwargs = dict(forecast=forecast, forecast_path=store.path, session_id=session_id,
        session_started=started, local_symbols=frozenset({"DOCU"}), account_fingerprint="a" * 64,
        quote_observed_at=at - timedelta(seconds=quote_age), max_quote_age_seconds=60,
        expected_time_in_force="DAY", manual_authority=lambda: state.manual,
        stopped=lambda: state.stop, authorization_current=lambda req, granted: state.proof,
        clock=lambda: state.now)
    return SimpleNamespace(store=store, forecast=forecast, request=request, grant=grant,
                           state=state, kwargs=kwargs)


def check(case, **changes):
    return gate.local_gate(case.request, case.grant, **{**case.kwargs, **changes})


def replace_request(case, **changes):
    case.request = replace(case.request, **changes)
    case.grant = replace(case.grant, request_id=case.request.request_id,
        request_sha256=case.request.sha256, account_fingerprint=case.request.account_fingerprint,
        session_id=case.request.session_id)


def save_newer(case, *, pc="Scout", mode="live", session=None, cycle="newer", symbol="DOCU"):
    value = {key: item for key, item in case.forecast.items() if key != "forecast_id"}
    value.update(pc=pc, mode=mode, cycle_id=cycle, symbol=symbol,
                 issued_at=(instant(value["issued_at"]) + timedelta(seconds=1)).isoformat())
    case.store.begin_cycle(cycle, dict(pc=pc, mode=mode,
        experiment=session or case.kwargs["session_id"], started_at=case.state.now.isoformat(),
        reference_at=value["reference_at"], symbols=[symbol], configuration_hash="c" * 64))
    return case.store.save_forecast(value)


@pytest.mark.parametrize("probability", [.72, .5, .2])
def test_exact_current_saved_scout_forecast_allows_capacity_reduction_without_store_construction(tmp_path, monkeypatch, probability):
    case = make_case(tmp_path, probability=probability)
    before = case.store.path.read_bytes()
    monkeypatch.setattr(ShadowStore, "__init__", lambda *a, **kw: pytest.fail("Production store constructed"))
    assert case.grant.quantity < case.request.quantity
    assert check(case) is True
    assert case.store.path.read_bytes() == before


@pytest.mark.parametrize("field,value", [("stop", True), ("stop", None), ("manual", False),
    ("manual", 1), ("proof", False), ("proof", 1), ("proof", None)])
def test_only_explicit_current_controls_and_proof_authorize(tmp_path, field, value):
    case = make_case(tmp_path)
    setattr(case.state, field, value)
    with pytest.raises(ContractError):
        check(case)


@pytest.mark.parametrize("field,value", [("pc", "Atlas"), ("mode", "shadow")])
def test_foreign_participant_and_nonlive_forecasts_are_refused(tmp_path, field, value):
    case = make_case(tmp_path, **{field: value})
    with pytest.raises(ContractError, match="local Scout live"):
        check(case)


@pytest.mark.parametrize("changes", [dict(session_id="different"), dict(account_fingerprint="c" * 64),
    dict(side="SELL"), dict(snapshot_sha256="c" * 64),
    dict(reference_at="2026-10-08T16:45:00Z"),
    dict(target_close_at="2026-10-08T18:00:00Z")])
def test_mutated_bound_request_cannot_use_a_rebound_grant(tmp_path, changes):
    case = make_case(tmp_path)
    replace_request(case, **changes)
    with pytest.raises(ContractError):
        check(case)


def test_grant_exact_request_mutation_is_rejected(tmp_path):
    case = make_case(tmp_path)
    case.request = replace(case.request, quantity=5)
    with pytest.raises(ContractError):
        check(case)


@pytest.mark.parametrize("field,value", [("local_symbols", frozenset({"DBX"})),
    ("max_quote_age_seconds", 0), ("max_quote_age_seconds", True),
    ("max_quote_age_seconds", float("nan")), ("expected_time_in_force", "PM"),
    ("session_started", "2026-10-08T17:16:05Z")])
def test_explicit_binding_and_evidence_configuration_required(tmp_path, field, value):
    case = make_case(tmp_path)
    with pytest.raises(ContractError):
        check(case, **{field: value})


@pytest.mark.parametrize("age", [-.001, 60.001])
def test_future_or_stale_quote_is_refused(tmp_path, age):
    case = make_case(tmp_path, quote_age=age)
    with pytest.raises(ContractError, match="quote"):
        check(case)


def test_configured_quote_age_boundary_is_inclusive(tmp_path):
    case = make_case(tmp_path, quote_age=60)
    assert check(case)


@pytest.mark.parametrize("now,reason", [("2026-10-08T17:16:01Z", "future"),
    ("2026-10-08T17:45:00Z", "target already elapsed"),
    ("2026-10-09T17:16:10Z", "wrong_session_date")])
def test_time_restrictions_are_checked_even_with_a_fresh_quote(tmp_path, now, reason):
    case = make_case(tmp_path)
    case.state.now = instant(now)
    with pytest.raises(ContractError, match=reason):
        check(case, quote_observed_at=case.state.now)


@pytest.mark.parametrize("reference,cutoff", [
    ("2026-10-08T23:15:00Z", "2026-10-08T23:45:00Z"),
    ("2026-11-06T00:15:00Z", "2026-11-06T00:45:00Z")])
def test_strict_1645_pacific_cutoff_before_and_after_dst(tmp_path, reference, cutoff):
    case = make_case(tmp_path, reference=reference)
    case.state.now = instant(cutoff) - timedelta(microseconds=1)
    assert check(case, quote_observed_at=case.state.now, expected_time_in_force="PM")
    case.state.now = instant(cutoff)
    with pytest.raises(ContractError, match="submission_cutoff"):
        check(case, quote_observed_at=case.state.now, expected_time_in_force="PM")


@pytest.mark.parametrize("reference", ["2026-10-10T17:00:00Z", "2026-10-08T08:00:00Z"])
def test_closed_calendar_or_clock_window_is_not_a_submission_permission(tmp_path, reference):
    case = make_case(tmp_path, reference=reference)
    with pytest.raises(ContractError, match="execution window"):
        check(case)


@pytest.mark.parametrize("which", ["request", "grant"])
def test_expired_send_permission_is_refused(tmp_path, which):
    case = make_case(tmp_path)
    if which == "request":
        replace_request(case, valid_until=case.state.now.isoformat())
    case.grant = replace(case.grant, expires_at=case.state.now.isoformat())
    with pytest.raises(ContractError, match="expired"):
        check(case)


def test_newer_saved_forecast_supersedes_before_queue_callback_runs(tmp_path):
    case = make_case(tmp_path)
    save_newer(case)
    with pytest.raises(ContractError, match="Newer current-session forecast"):
        check(case)


@pytest.mark.parametrize("changes", [dict(pc="Atlas"), dict(mode="shadow"),
    dict(session="another-session"), dict(symbol="DBX")])
def test_other_participant_mode_session_or_symbol_does_not_supersede(tmp_path, changes):
    case = make_case(tmp_path)
    save_newer(case, **changes)
    assert check(case)


@pytest.mark.parametrize("table,statement", [
    ("forecasts", "UPDATE forecasts SET symbol='DBX'"),
    ("forecasts", "UPDATE forecasts SET payload='{}'"),
    ("cycles", "UPDATE cycles SET pc='Atlas'"),
    ("cycles", "DELETE FROM cycles"),
    ("snapshots", "UPDATE snapshots SET payload='{}'"),
    ("symbol_inputs", "DELETE FROM symbol_inputs")])
def test_corrupted_saved_evidence_is_refused(tmp_path, table, statement):
    case = make_case(tmp_path)
    with case.store.connect() as db:
        db.execute(statement)
    with pytest.raises(ContractError):
        check(case)


def test_missing_database_is_not_created(tmp_path):
    case = make_case(tmp_path)
    missing = tmp_path / "absent.sqlite3"
    with pytest.raises(ContractError):
        check(case, forecast_path=missing)
    assert not missing.exists()


@pytest.mark.parametrize("path", ["relative.sqlite3", "//server/share/forecast.sqlite3"])
def test_final_gate_never_opens_relative_or_network_evidence(tmp_path, monkeypatch, path):
    case = make_case(tmp_path)
    monkeypatch.setattr(gate.sqlite3, "connect", lambda *args, **kw: pytest.fail("Unsafe evidence opened"))
    with pytest.raises(ContractError):
        check(case, forecast_path=path)


def test_database_lock_fails_closed_without_wait(tmp_path, monkeypatch):
    case = make_case(tmp_path)
    original_connect = sqlite3.connect
    seen = []
    def audited_connect(*args, **kwargs):
        seen.append((args, kwargs))
        return original_connect(*args, **kwargs)
    monkeypatch.setattr(gate.sqlite3, "connect", audited_connect)
    with original_connect(case.store.path) as writer:
        writer.execute("PRAGMA journal_mode=DELETE")
        writer.execute("BEGIN EXCLUSIVE")
        with pytest.raises(ContractError):
            check(case)
    assert len(seen) == 1
    assert seen[0][0][0].endswith("?mode=ro")
    assert seen[0][1] == dict(uri=True, timeout=0)


@pytest.mark.parametrize("mutation", ["stop", "proof", "stale", "newer"])
def test_final_controls_time_and_saved_latest_are_rechecked_after_initial_proof(tmp_path, mutation):
    case = make_case(tmp_path)
    count = 0
    def proof(request, grant):
        nonlocal count
        count += 1
        assert (request, grant) == (case.request, case.grant)
        if count == 1:
            if mutation == "stop":
                case.state.stop = True
            elif mutation == "proof":
                case.state.proof = False
            elif mutation == "stale":
                case.state.now += timedelta(seconds=61)
            else:
                save_newer(case)
            return True
        return case.state.proof
    with pytest.raises(ContractError):
        check(case, authorization_current=proof)


def test_query_only_is_enabled_and_no_store_mutation_occurs(tmp_path, monkeypatch):
    case = make_case(tmp_path)
    original = gate._cycle
    verified = []
    def audited_cycle(db, cycle_id):
        verified.append(db.execute("PRAGMA query_only").fetchone()[0])
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            db.execute("DELETE FROM forecasts")
        return original(db, cycle_id)
    monkeypatch.setattr(gate, "_cycle", audited_cycle)
    assert check(case)
    assert verified == [1]


def test_callback_cannot_change_validated_forecast_container(tmp_path):
    case = make_case(tmp_path)
    original_id = case.forecast["forecast_id"]
    def proof(request, grant):
        case.forecast["symbol"] = "DBX"
        return True
    # The detached original is still exactly saved and bound to this request.
    assert check(case, authorization_current=proof)
    assert case.request.forecast_id == original_id and case.forecast["symbol"] == "DBX"
    # A subsequent gate starts from the now-invalid supplied container.
    with pytest.raises(ContractError):
        check(case)


def test_stop_during_local_database_read_is_checked_before_return(tmp_path, monkeypatch):
    case = make_case(tmp_path)
    original = gate._saved_forecast
    def read(*args, **kwargs):
        original(*args, **kwargs)
        case.state.stop = True
    monkeypatch.setattr(gate, "_saved_forecast", read)
    with pytest.raises(ContractError, match="stopped"):
        check(case)


def test_stop_during_final_manual_status_read_is_checked_before_return(tmp_path):
    case = make_case(tmp_path)
    calls = 0
    def manual_authority():
        nonlocal calls
        calls += 1
        if calls == 2:
            case.state.stop = True
        return True
    with pytest.raises(ContractError, match="stopped"):
        check(case, manual_authority=manual_authority)


@pytest.mark.parametrize("changes", [dict(ensemble_status="incomplete"), dict(successful_count=1),
    dict(target_open="2026-10-08T17:45:00+00:00"), dict(target_close="2026-10-08T18:00:00+00:00"),
    dict(availability_basis="assumed_historical")])
def test_rehashed_invalid_local_forecast_cannot_supply_submission_eligibility(tmp_path, changes):
    case = make_case(tmp_path)
    updated = {**case.forecast, **changes}
    updated.pop("forecast_id")
    updated["forecast_id"] = digest(updated)
    case.forecast = updated
    case.kwargs["forecast"] = updated
    replace_request(case, forecast_id=updated["forecast_id"], forecast_sha256=updated["forecast_id"])
    with case.store.connect() as db:
        db.execute("UPDATE forecasts SET id=?,payload=?", (updated["forecast_id"], encoded(updated)))
    with pytest.raises(ContractError):
        check(case)
