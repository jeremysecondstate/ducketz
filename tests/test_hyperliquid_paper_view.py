"""Accounting, read-only behavior and partial-source coverage for H.Y.P.E.R."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from app.services.hyperliquid_paper_view import HyperliquidPaperViewService, filter_rows
from ml.hyperliquid_paper_ledger import PaperLedger


NOW = datetime(2026, 9, 26, 4, 0, tzinfo=timezone.utc).timestamp()


def utc(seconds=NOW):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def service(root, **kwargs):
    return HyperliquidPaperViewService(root, clock=lambda: NOW,
                                      process_probe=lambda *_: True, **kwargs)


@pytest.fixture
def paper_root(tmp_path):
    root = tmp_path / "hyperliquid"
    path = root / "_paper" / "ledger.sqlite3"
    # Only fixture setup constructs a writer. The display adapter must not.
    with PaperLedger(path, {"alex": 1000, "jeremy": 1000, "clearpond": 1000},
                     initial_positions=[{"account": "alex", "coin": "BTC", "kind": "perp",
                                         "quantity": -2, "avg_entry": 120}],
                     initial_marks={"perp:BTC": 100}, now=NOW-600) as ledger:
        common = {"account": "alex", "coin": "BTC", "kind": "perp", "qualified": False,
                  "p_not_down": .6, "current_notional": -200, "target_notional": 0,
                  "reason": "signal_rebalance", "forecast_id": "forecast1", "model_id": "model1"}
        ledger.execute_cycle("latest", NOW-1, {"perp:BTC": 100},
            [{**common, "quantity": 2, "price": 100, "fee_rate": .005,
              "requested_quantity": 3, "unfilled_quantity": 1}],
            transfers=[{"from_account": "jeremy", "to_account": "alex", "amount": 100,
                        "reason": "collateral_rebalance", "coin": "BTC"}],
            decisions=[{**common, "action": "fill", "execution": {"quantity": 2, "requested_quantity": 3}}])
    operational(root)
    return root


def operational(root, *, stamp=NOW-1):
    for path in (root / "_paper" / "_runtime" / "status.json",
                 root / "_models" / "_runtime" / "status.json",
                 root / "_coordinator" / "coordinator_status.json"):
        write_json(path, {"status": "running", "pid": 1234, "updated_at_utc": utc(stamp),
                          "portfolio": {"pooled": {"equity": 999999, "total_pnl": 888888}}})
    write_json(root / "_paper" / "performance.json", {"as_of_utc": utc(stamp-60), "max_drawdown_fraction": -.01})
    write_json(root / "_paper" / "policy.json", {"policy_id": "saved_policy", "pool_gross_fraction": .6})
    for coin in ("BTC", "ETH", "HYPE", "ZEC"):
        base = root / coin / "15m"
        write_json(base / "latest.json", {"run_id": "run1"})
        write_json(base / "runs" / "run1" / "summary.json", {"last_close_utc": utc(stamp)})
        write_json(base / "loop_status.json", {"status": "waiting", "updated_at_utc": utc(stamp),
            "last_result": {"last_close_utc": utc(stamp)}, "last_cycle_timing": {
                "finished_at_utc": utc(stamp), "total_seconds": .5, "queue_wait_seconds": .02,
                "timings_seconds": {"fetch_and_normalize": .3, "feature_build": .1,
                                    "parquet_and_catalog_write": .04, "total_before_publish": .48}}})
        model = root / "_models" / coin / "15m" / "h4"
        write_json(model / "latest_prediction.json", {"coin": coin, "model_id": "model1",
            "p_not_down": .6, "p_down": .4, "qualified": coin == "ETH", "created_at_utc": utc(stamp),
            "horizon_bars": 4, "target_close_utc": utc(stamp+3600)})
        write_json(model / "candidate.json", {"model_id": "model1", "trained_at_utc": utc(stamp-600)})
        write_json(model / "runs" / "model1" / "record.json", {"trained_at_utc": utc(stamp-600)})
        write_json(model / "runs" / "model1" / "report.json", {"splits": {
            "fit": {"last_decision_close_utc": utc(stamp-86400), "last_label_end_utc": utc(stamp-82800)},
            "calibration": {"last_decision_close_utc": utc(stamp-7200)}}})


def test_ledger_is_authoritative_pooled_not_double_counted_and_pnl_excludes_inherited_gains(paper_root):
    snapshot = service(paper_root).load_snapshot()
    assert snapshot.pooled["equity"] == pytest.approx(3039)
    assert snapshot.pooled["total_pnl"] == pytest.approx(-1)
    assert snapshot.pooled["net_realized_pnl"] == pytest.approx(39)
    assert snapshot.accounts["alex"]["equity"] == pytest.approx(1139)
    assert snapshot.accounts["alex"]["total_pnl"] == pytest.approx(-1)
    assert snapshot.accounts["jeremy"]["equity"] == pytest.approx(900)
    assert snapshot.accounts["jeremy"]["total_pnl"] == pytest.approx(0)
    assert snapshot.pooled["return_fraction"] == pytest.approx(-1 / 3040)
    assert "portfolio" not in snapshot.runtime
    assert snapshot.portfolio_observed_at_utc == utc(NOW-1)
    assert snapshot.policy["policy_id"] == "saved_policy"
    assert snapshot.seed["timestamp_utc"] == utc(NOW-600)


def test_seeded_portfolio_is_visible_before_any_strategy_cycle(tmp_path):
    database = tmp_path / "_paper" / "ledger.sqlite3"
    with PaperLedger(database, {"alex": 1000, "jeremy": 1000, "clearpond": 1000},
                     initial_positions=[{"account": "alex", "coin": "BTC", "kind": "perp",
                                         "quantity": -2, "avg_entry": 120}],
                     initial_marks={"perp:BTC": 100}, now=NOW) as ledger:
        seed = ledger.seed()
    snapshot = service(tmp_path).load_snapshot()
    assert snapshot.portfolio_observed_at_utc == utc()
    assert snapshot.pooled["equity"] == 3040
    assert snapshot.pooled["total_pnl"] == 0
    assert snapshot.pooled["fees"] == 0
    assert snapshot.positions[0]["avg_entry"] == 120
    assert snapshot.positions[0]["mark_price"] == 100
    assert snapshot.positions[0]["quantity"] == -2
    assert not snapshot.fills and not snapshot.decisions and not snapshot.transfers
    assert len(snapshot.equity_history) == 4
    assert snapshot.seed == seed


def test_legacy_seed_remains_auditable_without_fabricating_chart_observations(paper_root):
    database = paper_root / "_paper" / "ledger.sqlite3"
    # Model an older ledger that recorded only the first post-fill valuation.
    with sqlite3.connect(database) as connection:
        connection.execute("DELETE FROM equity WHERE cycle_id != 'latest'")
        connection.execute("DELETE FROM cycles WHERE cycle_id != 'latest'")
    snapshot = service(paper_root).load_snapshot()
    assert sum(snapshot.seed["baseline_equity"].values()) == 3040
    assert snapshot.seed["positions"][0]["quantity"] == -2
    assert snapshot.pooled["equity"] == 3039
    assert snapshot.pooled["total_pnl"] == -1
    assert {row["cycle_id"] for row in snapshot.equity_history} == {"latest"}
    assert {row["timestamp_utc"] for row in snapshot.equity_history} == {utc(NOW-1)}


@pytest.mark.parametrize("age,state", [(601, "fresh"), (1799, "fresh"), (1801, "stale")])
def test_performance_export_freshness_matches_fifteen_minute_schedule(paper_root, age, state):
    write_json(paper_root / "_paper" / "performance.json",
               {"as_of_utc": utc(NOW-age), "max_drawdown_fraction": -.5})
    snapshot = service(paper_root).load_snapshot()
    source = snapshot.sources["performance"]
    assert source.cadence_seconds == 900
    assert source.state == state
    assert "every 15 minutes" in source.detail
    assert snapshot.pooled["equity"] == 3039
    assert snapshot.equity_history[-1]["equity"] == 3039


@pytest.mark.parametrize("cadence,expected_cadence,expected_state", [
    (900, 900, "stale"), (3600, 3600, "fresh"),
    (None, 3600, "fresh"), (0, 3600, "fresh"), (True, 3600, "fresh"),
])
def test_training_freshness_uses_running_model_cadence(paper_root, cadence, expected_cadence, expected_state):
    runtime_path = paper_root / "_models" / "_runtime" / "status.json"
    runtime = json.loads(runtime_path.read_text())
    write_json(runtime_path, {**runtime, "retrain_seconds": cadence})
    write_json(paper_root / "_models" / "BTC" / "15m" / "h4" / "candidate.json",
               {"model_id": "model1", "trained_at_utc": utc(NOW-1801)})
    source = service(paper_root).load_snapshot().sources["training:BTC"]
    assert source.cadence_seconds == expected_cadence
    assert source.state == expected_state


def test_read_only_snapshot_does_not_construct_writer_or_change_authoritative_files(paper_root, monkeypatch):
    files = {p: hashlib.sha256(p.read_bytes()).digest() for p in paper_root.rglob("*") if p.is_file()}
    def forbidden(*args, **kwargs):
        raise AssertionError("Read view attempted to instantiate PaperLedger")
    monkeypatch.setattr(PaperLedger, "__init__", forbidden)
    snapshot = service(paper_root).load_snapshot()
    assert snapshot.sources["ledger"].state == "fresh"
    assert not snapshot.warnings
    assert {p: hashlib.sha256(p.read_bytes()).digest() for p in files} == files
    # SQLite itself can create coordination sidecars for a WAL-mode read-only
    # connection. Disabling WAL locking via immutable=1 is unsafe on a live DB.
    added = {p.name for p in paper_root.rglob("*") if p.is_file() and p not in files}
    assert added <= {"ledger.sqlite3-wal", "ledger.sqlite3-shm"}


def test_absent_root_stays_absent_and_balances_remain_missing(tmp_path):
    root = tmp_path / "not-created"
    snapshot = service(root).load_snapshot()
    assert not root.exists()
    assert snapshot.sources["ledger"].state == "missing"
    assert snapshot.sources["paper"].state == "missing"
    assert snapshot.pooled == {} and snapshot.accounts == {}
    assert not snapshot.equity_history and not snapshot.forecasts
    assert snapshot.warnings


def test_missing_ledger_does_not_fall_back_to_status_balances(tmp_path):
    operational(tmp_path)
    snapshot = service(tmp_path).load_snapshot()
    assert snapshot.pooled == {}
    assert snapshot.runtime["status"] == "running"
    assert snapshot.sources["ledger"].state == "missing"


def test_saved_running_status_cannot_hide_stale_sources(paper_root):
    snapshot = HyperliquidPaperViewService(paper_root, clock=lambda: NOW+10000,
                                         process_probe=lambda *_: True).load_snapshot()
    assert snapshot.sources["ledger"].state == "stale"
    assert snapshot.sources["paper"].state == "stale"
    assert snapshot.sources["forecast:BTC"].state == "stale"
    assert snapshot.sources["data:BTC"].state == "stale"
    assert snapshot.runtime["status"] == "stale"
    assert snapshot.runtime["reported_status"] == "running"
    assert snapshot.pooled["equity"] == pytest.approx(3039)


def test_dead_process_and_unknown_process_are_not_reported_running(paper_root):
    dead = HyperliquidPaperViewService(paper_root, clock=lambda: NOW,
                                      process_probe=lambda *_: False).load_snapshot()
    unknown = HyperliquidPaperViewService(paper_root, clock=lambda: NOW,
                                         process_probe=lambda *_: None).load_snapshot()
    assert dead.runtime["status"] == "stopped"
    assert unknown.runtime["status"] == "partial"
    assert unknown.runtime["process_alive"] is None


def test_prepared_opening_reports_intentional_stop_without_claiming_running(paper_root):
    write_json(paper_root / "_paper" / "_runtime" / "status.json",
               {"status": "stopped", "pid": 1234, "updated_at_utc": utc(),
                "prepare_only": True, "stop_reason": "prepare_only", "lifecycle_phase": "opening_prepared"})
    snapshot = HyperliquidPaperViewService(paper_root, clock=lambda: NOW,
                                          process_probe=lambda *_: False).load_snapshot()
    assert snapshot.runtime["status"] == "stopped"
    assert snapshot.sources["paper"].detail == "Opening prepared; stopped intentionally before strategy cycles"


def test_partial_json_and_source_errors_preserve_valid_ledger(paper_root):
    (paper_root / "_models" / "BTC" / "15m" / "h4" / "latest_prediction.json").write_text('{"partial":', encoding="utf-8")
    write_json(paper_root / "_paper" / "_runtime" / "status.json", {
        "status": "degraded", "pid": 1234, "updated_at_utc": utc(),
        "last_error": "Missing mark for held position: perp:ETH", "quote_errors": {"ETH": "book unavailable"}})
    snapshot = service(paper_root).load_snapshot()
    assert snapshot.sources["paper"].state == "partial"
    assert snapshot.sources["forecast:BTC"].state == "partial"
    assert len(snapshot.forecasts) == 3
    assert snapshot.pooled["equity"] == pytest.approx(3039)
    assert snapshot.runtime["quote_errors"] == {"ETH": "book unavailable"}


def test_partial_ledger_can_show_recorded_equity_without_inventing_marks(paper_root):
    with sqlite3.connect(paper_root / "_paper" / "ledger.sqlite3") as connection:
        connection.execute("DROP TABLE cycles")
        connection.execute("INSERT INTO positions VALUES ('jeremy','ETH','perp',1,200,200,'fill_price')")
    snapshot = service(paper_root).load_snapshot()
    assert snapshot.pooled["equity"] == pytest.approx(3039)
    assert snapshot.sources["ledger"].state == "partial"
    position = snapshot.positions[0]
    assert position["coin"] == "ETH"
    assert position.get("mark_price") is None
    assert position.get("unrealized_pnl") is None


def test_filters_respect_accounts_assets_qualification_and_passive_positions(paper_root):
    snapshot = service(paper_root).load_snapshot()
    assert len(filter_rows(snapshot.fills, account="Alex", asset="BTC", qualification="research")) == 1
    assert not filter_rows(snapshot.fills, qualification="qualified")
    assert not filter_rows(snapshot.fills, account="Jeremy")
    assert len(filter_rows(snapshot.transfers, account="Jeremy")) == 1
    assert len(filter_rows(snapshot.transfers, account="Alex")) == 1
    assert not filter_rows(snapshot.transfers, account="Clear Pond")
    passive = {"account": "alex", "coin": "HYPE", "passive": True}
    assert not filter_rows([passive], include_passive=False)
    assert filter_rows([{"coin": "ETH", "action": "skip"}], account="Alex")


def test_partial_execution_and_committed_transfers_keep_actual_evidence(paper_root):
    snapshot = service(paper_root).load_snapshot()
    fill = snapshot.fills[0]
    assert fill["quantity"] == 2
    assert fill["requested_quantity"] == 3
    assert fill["unfilled_quantity"] == 1
    assert fill["qualification"] == "research"
    assert fill["details"]["qualified"] is False
    assert snapshot.transfers[0]["status"] == "committed"
    assert snapshot.decisions[0]["execution"]["quantity"] == 2


def test_equity_history_is_per_account_and_transfer_adjusted(paper_root):
    snapshot = service(paper_root).load_snapshot()
    pooled = [row for row in snapshot.equity_history if row["account"] == "pooled"]
    jeremy = [row for row in snapshot.equity_history if row["account"] == "jeremy"]
    assert [row["equity"] for row in pooled] == pytest.approx([3040, 3039])
    assert pooled[-1]["drawdown_fraction"] == pytest.approx(-1 / 3040)
    # An internal cash transfer is not a drawdown or negative strategy return.
    assert jeremy[-1]["equity"] == 900
    assert jeremy[-1]["drawdown_fraction"] == 0


def test_history_sampling_covers_full_period_and_keeps_last_observation(paper_root):
    database = paper_root / "_paper" / "ledger.sqlite3"
    with sqlite3.connect(database) as connection:
        template = connection.execute("SELECT * FROM equity WHERE account='pooled' ORDER BY rowid LIMIT 1").fetchone()
        for index in range(40):
            row = list(template)
            row[0], row[1], row[4], row[10] = f"test-{index}", utc(NOW+index), 3040+index, index
            connection.execute("INSERT INTO equity VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", row)
    snapshot = service(paper_root, history_limit=12).load_snapshot()
    pool = [row for row in snapshot.equity_history if row["account"] == "pooled"]
    assert snapshot.history_sampled
    assert len(snapshot.equity_history) <= 12
    assert pool[0]["timestamp_utc"] == utc(NOW-600)
    assert pool[-1]["equity"] == 3079
    assert pool[-1]["total_pnl"] == 39
    assert all(row["equity"] <= 3079 for row in pool)


@pytest.mark.parametrize("limit", [8, 12, 20])
def test_history_sampling_preserves_opening_before_near_immediate_loss(tmp_path, limit):
    database = tmp_path / "_paper" / "ledger.sqlite3"
    with PaperLedger(database, {"alex": 1000, "jeremy": 1000, "clearpond": 1000}, now=NOW) as ledger:
        for index in range(1, 31):
            ledger.execute_cycle(str(index), NOW + index / 100, {}, [],
                                 funding=[{"funding_id": f"f{index}", "account": "alex", "coin": "BTC", "amount": -1}])
    snapshot = service(tmp_path, history_limit=limit).load_snapshot()
    assert snapshot.history_sampled
    assert len(snapshot.equity_history) <= limit
    for account in ("alex", "jeremy", "clearpond", "pooled"):
        rows = [row for row in snapshot.equity_history if row["account"] == account]
        assert rows[0]["timestamp_utc"] == utc()
        assert rows[0]["total_pnl"] == 0
        assert rows[-1]["timestamp_utc"] == utc(NOW+.3)
    pooled = [row for row in snapshot.equity_history if row["account"] == "pooled"]
    assert pooled[-1]["drawdown_fraction"] == pytest.approx(-30 / 3000)


def test_journal_limit_is_bounded_and_keeps_newest(paper_root):
    with sqlite3.connect(paper_root / "_paper" / "ledger.sqlite3") as connection:
        row = list(connection.execute("SELECT * FROM decisions").fetchone())
        for index in range(5):
            row[0], row[2] = f"decision{index}", utc(NOW+index)
            connection.execute("INSERT INTO decisions VALUES (?,?,?,?,?,?,?,?,?,?)", row)
    snapshot = service(paper_root, journal_limit=2).load_snapshot()
    assert [row["decision_id"] for row in snapshot.decisions] == ["decision4", "decision3"]


def test_timings_separate_worker_queue_poll_and_publication_and_keep_provenance(paper_root):
    path = paper_root / "_models" / "_runtime" / "training_events.jsonl"
    event = {"coin": "BTC", "at_utc": utc(), "timing": {"worker_seconds": 1.3,
        "queue_wait_seconds": .01, "collection_wait_seconds": 3.6, "publication_seconds": .04,
        "end_to_end_seconds": 4.95}, "model_timings": {"one": {"fit_seconds": .8,
        "calibration_seconds": .2, "assessment_seconds": .1}}}
    path.write_text(json.dumps(event) + '\n{"unfinished":', encoding="utf-8")
    snapshot = service(paper_root).load_snapshot()
    model = next(row for row in snapshot.timings if row["kind"] == "model")
    data = next(row for row in snapshot.timings if row["kind"] == "data")
    assert model["work_seconds"] == 1.3
    assert model["poll_wait_seconds"] == 3.6
    assert model["publication_seconds"] == .04
    assert model["fit_seconds"] == .8
    assert data["work_seconds"] == .48
    assert data["cycle_seconds"] == .5
    assert data["queue_wait_seconds"] == .02
    assert data["feature_seconds"] == .1
    assert data["publication_seconds"] is None  # Not independently measured.
    forecast = snapshot.forecasts[0]
    assert forecast["horizon_minutes"] == 60
    assert forecast["training_cutoff_utc"] != forecast["model_published_at_utc"]
    assert forecast["qualification"] == "research"
    assert any("incomplete timing" in warning for warning in snapshot.warnings)


def test_invalid_probability_is_missing_not_a_zero_forecast(paper_root):
    path = paper_root / "_models" / "BTC" / "15m" / "h4" / "latest_prediction.json"
    write_json(path, {"p_not_down": 7, "created_at_utc": utc()})
    snapshot = service(paper_root).load_snapshot()
    assert snapshot.forecasts[0]["p_not_down"] is None
    assert snapshot.sources["forecast:BTC"].state == "partial"


def test_corrupt_sqlite_returns_error_state_without_creating_any_schema(tmp_path):
    database = tmp_path / "_paper" / "ledger.sqlite3"
    database.parent.mkdir()
    database.write_bytes(b"this is not a database")
    snapshot = service(tmp_path).load_snapshot()
    assert snapshot.sources["ledger"].state == "error"
    assert snapshot.pooled == {}
    assert database.read_bytes() == b"this is not a database"


def test_malformed_journal_detail_keeps_actual_fill_and_marks_partial(paper_root):
    with sqlite3.connect(paper_root / "_paper" / "ledger.sqlite3") as connection:
        connection.execute("UPDATE fills SET details_json='[]'")
    snapshot = service(paper_root).load_snapshot()
    assert snapshot.fills[0]["quantity"] == 2
    assert snapshot.fills[0]["details_unavailable"]
    assert snapshot.fills[0]["qualification"] == "unavailable"
    assert snapshot.sources["ledger"].state == "partial"


def test_mismatched_cycle_and_fallback_equity_never_mix_position_marks(paper_root):
    with sqlite3.connect(paper_root / "_paper" / "ledger.sqlite3") as connection:
        # Leave the latest cycle at the original opening but force the fallback
        # to later, post-transfer equity rows. Reuse the same position quantity
        # to ensure quantity matching cannot accidentally justify an old mark.
        connection.execute("DELETE FROM cycles WHERE cycle_id='latest'")
        connection.execute("DELETE FROM equity WHERE cycle_id!='latest'")
        connection.execute("INSERT INTO positions VALUES ('alex','BTC','perp',-2,120,120,'legacy_avg_entry')")
    snapshot = service(paper_root).load_snapshot()
    assert snapshot.portfolio_observed_at_utc == utc(NOW-1)
    assert snapshot.pooled["equity"] == pytest.approx(3039)
    assert "positions" not in snapshot.pooled
    assert "net_realized_pnl" not in snapshot.pooled
    assert "positions" not in snapshot.accounts["alex"]
    assert snapshot.accounts["alex"]["net_transfers"] == pytest.approx(100)
    assert snapshot.positions[0]["quantity"] == -2
    assert snapshot.positions[0].get("mark_price") is None
    assert snapshot.positions[0].get("notional") is None
    assert snapshot.positions[0].get("unrealized_pnl") is None
    assert snapshot.sources["ledger"].state == "partial"
    assert any("observations differ" in warning for warning in snapshot.warnings)


@pytest.mark.parametrize("down", [.6, 1.5, None, True, "not-a-number"])
def test_inconsistent_complementary_probability_withholds_both_values(paper_root, down):
    path = paper_root / "_models" / "BTC" / "15m" / "h4" / "latest_prediction.json"
    write_json(path, {"p_not_down": .6, "p_down": down, "created_at_utc": utc(), "horizon_bars": 4})
    snapshot = service(paper_root).load_snapshot()
    forecast = snapshot.forecasts[0]
    assert forecast["p_not_down"] is None
    assert forecast["p_down"] is None
    assert snapshot.sources["forecast:BTC"].state == "partial"
    assert forecast["validation_errors"]


def test_omitted_complementary_probability_is_not_fabricated(paper_root):
    path = paper_root / "_models" / "BTC" / "15m" / "h4" / "latest_prediction.json"
    write_json(path, {"p_not_down": .6, "created_at_utc": utc(), "horizon_bars": 4})
    snapshot = service(paper_root).load_snapshot()
    assert snapshot.forecasts[0]["p_not_down"] == .6
    assert snapshot.forecasts[0]["p_down"] is None
    assert snapshot.sources["forecast:BTC"].state == "fresh"


@pytest.mark.parametrize("bars", [None, 0, -1, 2.5, 8, "unknown"])
def test_unknown_or_mismatched_horizon_remains_unavailable(paper_root, bars):
    path = paper_root / "_models" / "BTC" / "15m" / "h4" / "latest_prediction.json"
    write_json(path, {"p_not_down": .6, "p_down": .4, "created_at_utc": utc(), "horizon_bars": bars})
    snapshot = service(paper_root).load_snapshot()
    forecast = snapshot.forecasts[0]
    assert forecast["p_not_down"] == .6
    assert forecast["horizon_bars"] is None
    assert forecast["horizon_minutes"] is None
    assert snapshot.sources["forecast:BTC"].state == "partial"
