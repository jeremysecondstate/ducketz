"""Offline evidence tests: no provider, training or trading dependencies."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from tools.intraday_model_diagnostics import diagnose


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "intraday").mkdir()
        self.database = self.root / "intraday/shadow.sqlite3"
        self.profile, self.report = self.root / "profile.json", self.root / "report.json"
        self.profile.write_text(json.dumps({"actor": "Atlas", "symbols": ["AAPL"]}))
        self.report_value = {"actor": "Atlas", "run_id": "fixture", "previous_session": {
            "session_id": "2026-10-15", "opened_at": "2026-10-15T08:00:00+00:00", "closed_at": "2026-10-16T00:00:00+00:00"},
            "symbols": {"AAPL": {"training_snapshot": {"training_rows": 100, "synthetic_labels_included": 0,
                "synthetic_labels_excluded": 0, "synthetic_policy": "include"}}}}
        self.save_report()
        with closing(sqlite3.connect(self.database)) as db:
            db.executescript("""
            CREATE TABLE forecasts(id TEXT PRIMARY KEY,pc TEXT,symbol TEXT,reference_at TEXT,issued_at TEXT,payload TEXT);
            CREATE INDEX forecasts_target ON forecasts(pc,symbol,reference_at);
            CREATE TABLE outcomes(id TEXT PRIMARY KEY,forecast_id TEXT,payload TEXT);
            CREATE INDEX outcomes_forecast ON outcomes(forecast_id);
            CREATE TABLE raw_originals(id TEXT PRIMARY KEY,payload BLOB);
            INSERT INTO raw_originals VALUES('original',X'000102FF');
            """)
            db.commit()

    def save_report(self):
        self.report.write_text(json.dumps(self.report_value))

    def add(self, key, *, issued="2026-10-15T14:29:59+00:00", status="scored", label=True,
            p=.8, observed=15, actor="Atlas", symbol="AAPL", reference="2026-10-15T14:00:00+00:00",
            availability="actual", mode="live"):
        forecast = {"forecast_id": key, "pc": actor, "symbol": symbol, "mode": mode,
            "reference_at": reference, "issued_at": issued, "target_open": "2026-10-15T14:30:00+00:00",
            "target_close": "2026-10-15T14:45:00+00:00", "probability_not_down": p,
            "availability_basis": "observed", "reference_provenance": {
                "observed_constituents": observed, "synthetic_constituents": 15-observed} if observed is not None else {}}
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("INSERT INTO forecasts VALUES(?,?,?,?,?,?)", (key, actor, symbol, reference, issued, json.dumps(forecast)))
            if status is not None:
                outcome = {"forecast_id": key, "status": status, "target_not_down": label, "availability_basis": availability}
                db.execute("INSERT INTO outcomes VALUES(?,?,?)", (key + "-o", key, json.dumps(outcome)))

    def run_diagnostics(self):
        return diagnose(self.root, self.profile, self.report)

    def test_boundaries_and_denominators_do_not_turn_synthetic_or_late_results_into_timely_accuracy(self):
        self.add("early", p=.8)
        self.add("at-open", issued="2026-10-15T14:30:00+00:00", label=False, p=.6)
        self.add("before-close", issued="2026-10-15T14:44:59+00:00", label=False, p=.2)
        self.add("at-close", issued="2026-10-15T14:45:00+00:00", p=.9)
        self.add("synthetic", status="synthetic", observed=0, p=1)
        self.add("absent", status=None, observed=7)
        self.add("pending", status="pending", label=None)
        self.add("unknown", availability="legacy_unknown", observed=None)
        self.add("reconstructed", availability="assumed_historical_close")
        result = self.run_diagnostics()
        cohorts = result["cohorts"]
        self.assertEqual(cohorts["timely_observed"]["scored"], 1)
        self.assertAlmostEqual(cohorts["timely_observed"]["brier"], .04)
        self.assertEqual(cohorts["during_target_observed"]["scored"], 2)
        self.assertAlmostEqual(cohorts["during_target_observed"]["brier"], .2)
        self.assertEqual(cohorts["after_close_observed"]["scored"], 1)
        self.assertEqual(cohorts["synthetic"]["scored"], 1)
        self.assertEqual(cohorts["historical_reconstructed"]["scored"], 1)
        self.assertEqual(cohorts["missing_outcome"]["forecasts"], 2)
        self.assertEqual(cohorts["missing_outcome"]["scored"], 0)
        self.assertEqual(result["all_saved_only"]["forecasts"], 9)
        self.assertEqual(result["all_saved_only"]["scored"], 7)
        self.assertEqual(sum(v["forecasts"] for v in cohorts.values()), 9)
        symbol = result["symbols"]["AAPL"]
        self.assertEqual(symbol["input_minute_coverage"], {"zero_observed": 1, "partly_observed": 1, "fully_observed": 6, "unknown": 1})
        self.assertIn("no_synthetic_training_labels", symbol["training_live_support"])
        self.assertEqual(symbol["session_period"], {"regular": 0, "extended": 0, "unknown": 9})
        self.assertIsNone(result["all_saved_only"]["causal_baseline"])

    def test_scope_and_offset_timestamps_use_exact_session_while_ignoring_peer_and_other_traders(self):
        self.add("local")
        self.add("peer", actor="Scout")
        self.add("other-symbol", symbol="GLOB")
        self.add("research", mode="shadow")
        self.add("before", reference="2026-10-15T07:59:59+00:00")
        self.add("close", reference="2026-10-16T00:00:00+00:00")
        self.add("offset", reference="2026-10-15T07:00:00-07:00")
        self.report_value["symbols"]["GLOB"] = self.report_value["symbols"]["AAPL"]
        self.save_report()
        result = self.run_diagnostics()
        self.assertEqual(set(result["symbols"]), {"AAPL"})
        self.assertEqual(result["all_saved_only"]["forecasts"], 2)
        self.assertEqual({r["forecast_id"] for r in result["symbols"]["AAPL"]["forecast_classifications"]}, {"local", "offset"})

    def test_latest_correction_is_read_without_rewriting_history_or_any_original_bytes(self):
        self.add("corrected", label=True, p=.8)
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("INSERT INTO outcomes VALUES(?,?,?)", ("correction", "corrected", json.dumps({
                "forecast_id": "corrected", "status": "scored", "target_not_down": False, "availability_basis": "actual"})))
        before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (self.database, self.report, self.profile)}
        result = self.run_diagnostics()
        self.assertAlmostEqual(result["cohorts"]["timely_observed"]["brier"], .64)
        self.assertEqual(before, {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in before})
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT payload FROM raw_originals").fetchone()[0], b"\x00\x01\x02\xff")

    def test_explicit_regular_session_bounds_only(self):
        self.add("regular")
        self.add("extended", reference="2026-10-15T08:15:00+00:00")
        self.report_value["previous_session"].update(regular_opened_at="2026-10-15T13:30:00+00:00", regular_closed_at="2026-10-15T20:00:00+00:00")
        self.save_report()
        self.assertEqual(self.run_diagnostics()["symbols"]["AAPL"]["session_period"], {"regular": 1, "extended": 1, "unknown": 0})

    def test_malformed_and_missing_inputs_fail_explicitly(self):
        self.report_value["actor"] = "Scout"
        self.save_report()
        with self.assertRaisesRegex(ValueError, "actor"):
            self.run_diagnostics()
        self.report.write_text("not JSON")
        with self.assertRaises(ValueError):
            self.run_diagnostics()
        self.report_value["actor"] = "Atlas"
        self.save_report()
        self.database.unlink()
        with self.assertRaisesRegex(ValueError, "missing"):
            self.run_diagnostics()

    def test_bad_saved_probability_is_an_unscored_unknown_with_error(self):
        self.add("broken", p=2)
        result = self.run_diagnostics()
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["cohorts"]["unknown"]["forecasts"], 1)
        self.assertEqual(result["all_saved_only"]["scored"], 0)
        self.assertIn("probability", result["errors"][0]["error"])

    def test_unsupported_input_coverage_and_receipt_provenance_remain_unknown(self):
        self.add("legacy", availability="not_recorded", observed=None)
        self.add("bad-counts", observed=16)
        result = self.run_diagnostics()
        self.assertEqual(result["symbols"]["AAPL"]["input_minute_coverage"]["unknown"], 2)
        self.assertEqual(result["cohorts"]["unknown"]["forecasts"], 1)
        self.assertEqual(result["cohorts"]["timely_observed"]["forecasts"], 1)

    def test_missing_index_is_an_explicit_database_error_and_never_created(self):
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("DROP INDEX outcomes_forecast")
        before = self.database.read_bytes()
        with self.assertRaises(sqlite3.OperationalError):
            self.run_diagnostics()
        self.assertEqual(self.database.read_bytes(), before)

    def test_invalid_training_counts_are_not_reported_as_observed_only(self):
        self.report_value["symbols"]["AAPL"]["training_snapshot"]["synthetic_labels_included"] = False
        self.save_report()
        with self.assertRaisesRegex(ValueError, "synthetic_labels_included"):
            self.run_diagnostics()


if __name__ == "__main__":
    unittest.main()
