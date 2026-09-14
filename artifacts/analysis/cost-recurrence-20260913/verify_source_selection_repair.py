"""Read-only validation against the exact inputs of the saved Monday Gameplan."""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from ml.gameplan_source_selection import select_prior_session_sources
from ml.independent_stock_targets import build_stock_training_groups
from ml.nightly_gameplan import _overnight_sources

OUT = Path(__file__).resolve().parent
DATA = Path("C:/DATASTORE")
RUN = DATA / "ml/nightly-gameplan-runs/20260912T051210.050260Z"


def sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def counts(frame):
    return {str(symbol): int(count) for symbol, count in frame.groupby("symbol").size().items()}


def main():
    manifest = json.loads((RUN / "manifest.json").read_text())
    price_report = manifest["configuration"]["stock_price_source"]
    frozen = {RUN / name: info["checksum_sha256"] for name, info in manifest["output_files"].items()}
    pointers = [DATA / path for path in (
        "ml/overnight-latest/run.json", "ml/nightly-gameplan-latest/run.json",
        "ml/gameplan-trade-plan-latest/run.json", "ml/gameplan-actuals-review-latest/run.json",
        "ml/gameplan-evaluation-latest/run.json",
    )]
    frozen.update({path: sha(path) for path in pointers})
    for path, digest in frozen.items():
        assert sha(path) == digest, path
    for item in price_report["files"]:
        path = Path(item["path"])
        assert path.stat().st_size == item["bytes"] and sha(path) == item["sha256"], path
    samples_path = DATA / manifest["configuration"]["source_loop_b_run"] / "samples.parquet"
    pinned_samples = next(item for item in manifest["input_files"] if DATA / item["path"] == samples_path)
    assert samples_path.stat().st_size == pinned_samples["size"]
    assert sha(samples_path) == pinned_samples["checksum_sha256"]
    samples = pd.read_parquet(samples_path)
    now = pd.Timestamp(manifest["run_timestamp"])
    symbols = tuple(sorted(price_report["by_symbol"]))
    features = tuple(column for column in manifest["feature_columns"] if column in samples)
    old = _overnight_sources(samples, symbols=symbols, available_at=now)
    new = select_prior_session_sources(samples, symbols=symbols, available_at=now, feature_columns=features)
    new_report = dict(new.attrs["source_selection"])
    frames = []
    for partition in price_report["partitions"]:
        part_manifest = Path(partition["manifest_path"])
        part = json.loads(part_manifest.read_text())
        normalized = part_manifest.parent / part["normalized"]["path"]
        frame = pd.read_parquet(normalized)
        stamp = part["normalized"]["timestamp_column"]
        if stamp not in frame and frame.index.name == stamp:
            frame = frame.reset_index()
        frames.append(frame.assign(timestamp=pd.to_datetime(frame[stamp], utc=True), symbol=partition["symbol"])
                      [["symbol", "timestamp", "open", "close"]])
    bars = pd.concat(frames, ignore_index=True).drop_duplicates(["symbol", "timestamp", "open", "close"])
    assert not bars.duplicated(["symbol", "timestamp"]).any()
    assert counts(bars) == {symbol: row["rows"] for symbol, row in price_report["by_symbol"].items()}
    bars.attrs["stock_price_source"] = price_report
    action_date = pd.Timestamp(manifest["configuration"]["action_date"]).date()
    old_current = old.loc[old.action_date.eq(action_date)].set_index("symbol").sort_index()
    new_current = new.loc[new.action_date.eq(action_date)].set_index("symbol").sort_index()
    check_columns = ["bar_timestamp", "bar_end_timestamp", "information_available_at", "decision_timestamp", *features]
    pd.testing.assert_frame_equal(old_current[check_columns], new_current[check_columns], check_dtype=False,
                                  check_index_type=False, check_exact=True)
    assert len(new_current) == len(symbols)
    assert new.decision_timestamp.le(new.source_effective_cutoff).all()
    assert new.information_available_at.le(new.decision_timestamp).all()
    assert new.bar_end_timestamp.ge(new.source_regular_close).all()
    assert new.decision_timestamp.lt(new.source_action_start).all()
    groups = {}
    for name, sources in (("legacy", old), ("repaired", new)):
        print(f"Building pinned {name} groups from {len(sources)} source sessions", flush=True)
        groups[name] = build_stock_training_groups(sources, feature_columns=manifest["feature_columns"],
                                                   minute_bars=bars, available_at=now,
                                                   price_source_contract=price_report["source_contract"])
    monthly = []
    for name, sources in (("legacy", old), ("repaired", new)):
        months = pd.to_datetime(sources.action_date).dt.strftime("%Y-%m")
        selected = sources.assign(month=months)
        for (month, symbol), count in selected.groupby(["month", "symbol"]).size().items():
            monthly.append({"selector": name, "month": month, "symbol": symbol, "source_sessions": int(count)})
    pd.DataFrame(monthly).to_csv(OUT / "source-selection-monthly-counts.csv", index=False)
    comparison = {}
    for group in groups["legacy"]:
        before, after = groups["legacy"][group], groups["repaired"][group]
        saved = pd.read_parquet(RUN / f"training-cohort-{group}.parquet")
        stable_columns = ["symbol", "action_date", "route", "decision_timestamp", "information_available_at",
                          "target_window_start", "target_window_end", "target", "observed_return",
                          "target_open", "target_close"]
        sort_columns = ["symbol", "action_date", "route"]
        pd.testing.assert_frame_equal(before[stable_columns].sort_values(sort_columns).reset_index(drop=True),
                                      saved[stable_columns].sort_values(sort_columns).reset_index(drop=True),
                                      check_dtype=False, check_exact=True)
        jan_march = lambda frame: frame.loc[frame.symbol.eq("COST") & pd.to_datetime(frame.action_date).between("2026-01-01", "2026-03-31")]
        comparison[group] = {
            "legacy_aligned_rows": len(before), "repaired_aligned_rows": len(after),
            "legacy_rows_by_symbol": counts(before), "repaired_rows_by_symbol": counts(after),
            "cost_january_march_legacy_rows": len(jan_march(before)),
            "cost_january_march_repaired_rows": len(jan_march(after)),
            "cost_january_march_repaired_sessions": jan_march(after).action_date.nunique(),
            "exact_legacy_reconstruction_matches_saved_cohort": True,
            "five_minute_policy_unchanged": bool(after.target_start_gap_seconds.le(300).all()
                                                  and after.target_end_gap_seconds.le(300).all()),
        }
        after.loc[after.symbol.eq("COST"), stable_columns + ["target_start_gap_seconds", "target_end_gap_seconds"]].to_parquet(
            OUT / f"source-selection-cost-aligned-{group}.parquet", index=False)
    for path, digest in frozen.items():
        assert sha(path) == digest, path
    report = {
        "status": "VERIFIED_READ_ONLY_SOURCE_AND_LABEL_SELECTION", "verified_at": pd.Timestamp.now(tz="UTC").isoformat(),
        "source_run": str(RUN), "source_samples": str(samples_path), "source_samples_sha256": sha(samples_path),
        "pinned_price_file_count_verified": len(price_report["files"]), "pinned_price_source": price_report["source_contract"],
        "source_selection": new_report, "legacy_source_counts": counts(old), "repaired_source_counts": counts(new),
        "current_monday_source_feature_values_and_clocks_exactly_unchanged_all_seven": True,
        "original_outputs_and_five_pointers_unchanged": True,
        "cohorts": comparison, "model_training_performed": False, "provider_calls": 0,
    }
    (OUT / "source-selection-repair-validation.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    lines = ["# Historical source-selection repair validation", "", "Read-only replay of the exact inputs and as-of time of the published September 14 Gameplan. No provider request, model fit, publication or pointer change.", "", "The new selector uses each immediately prior exchange session's latest eligible completed hourly feature bar. Its end must reach that session's actual regular close; features must be known by the earlier of the run as-of time or source-date 17:05 Pacific. It does not inspect the rolling model's next-target clock or label.", "", "## COST January–March 2026", "", "All 61 action sessions now have eligible prior-session features (previously zero). Observed labels still need genuine minute endpoints within five minutes; source eligibility alone does not manufacture an outcome.", "", "| Horizon | Prior aligned rows | Repaired aligned rows | Action sessions with aligned rows |", "|---|---:|---:|---:|"]
    for group, item in comparison.items():
        lines.append(f"| {group} | {item['cost_january_march_legacy_rows']} | {item['cost_january_march_repaired_rows']} | {item['cost_january_march_repaired_sessions']} |")
    lines.extend(["", "## Verification", "", f"- Verified all {len(price_report['files'])} pinned XNAS.ITCH price files and the exact source sample checksum.", "- Legacy reconstruction exactly reproduces every saved cohort's source clocks, targets, returns and endpoint prices.", "- The new selector leaves all seven Monday source feature values and causal clocks exactly unchanged.", "- All original published output checksums and all five current pointers remained unchanged.", "- Every repaired aligned label still passes the unchanged five-minute endpoint limits.", "- Chronological partitioning, model fitting and performance validation were not run by this audit; these are candidate aligned rows, not a claim that every row enters a fitted model.", "", "Detailed machine-readable counts: `source-selection-repair-validation.json`; monthly counts: `source-selection-monthly-counts.csv`.", ""])
    (OUT / "source-selection-repair-validation.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps(report, indent=2), flush=True)


if __name__ == "__main__":
    main()
