"""Refresh only the review wording; preserve the immutable v3 numerical plan."""
from copy import deepcopy
import json
from pathlib import Path

import pandas as pd

from ml.artifacts import file_checksum
from ml.gameplan_trade_review import render_trade_review


ROOT = Path("C:/dev/ducketz")
RUN = Path("C:/DATASTORE/ml/gameplan-trade-plan-runs/20260909T071232.777557Z")
SOURCE = Path("C:/DATASTORE/ml/nightly-gameplan-runs/20260909T060404.450224Z")
REVIEW = ROOT / "artifacts/gameplans/2026-09-09/Gameplan-2026-09-09-trade-review.md"
EXPECTED = "2a781fbd8b3c0e060b5382ce2c5b1c2983ed51432b37043312f2c5a9e36d60b2"
ASSUMPTION = (
    "The cash projection uses estimated prices and assumed earlier sale proceeds for its planned trades and horizon exits. "
    "Price and cash ranges never gate execution: live orders use the current tradable quote and actual available cash and "
    "holdings, including outside those estimates. Live purchases may use actual completed sale proceeds only when available at the broker."
)
PATH_POLICY = (
    "Price and cash estimates never gate execution. Use the current tradable quote and actual available cash and holdings, "
    "including when they fall outside the estimates."
)


def main():
    assert file_checksum(REVIEW) == EXPECTED, "Derived review changed; inspect before replacing it"
    native_paths = [RUN / name for name in ("receipt.json", "report.json", "trade-plan.parquet", "Gameplan.md")]
    native_paths += [SOURCE / name for name in ("receipt.json", "forecasts.parquet", "model-reports.json")]
    native_hashes = {str(path): file_checksum(path) for path in native_paths}
    rows = pd.read_parquet(RUN / "trade-plan.parquet")
    original_report = json.loads((RUN / "report.json").read_text(encoding="utf-8"))
    report = deepcopy(original_report)
    assumptions = report["direction_based_projection"]["assumptions"]
    matches = [i for i, text in enumerate(assumptions) if text.startswith("Every listed trade and horizon exit")]
    assert len(matches) == 1
    assumptions[matches[0]] = ASSUMPTION
    report["planning_price_path"]["market_gap_policy"] = PATH_POLICY
    report["planning_price_path"]["working_range_semantics"] = (
        "Estimated planning prices only; not a future confidence interval, guaranteed execution, or order-limit authority"
    )
    model_reports = json.loads((SOURCE / "model-reports.json").read_text(encoding="utf-8"))
    rendered = render_trade_review(rows, report, model_reports, source_gameplan=SOURCE.as_posix())
    previous = REVIEW.read_text(encoding="utf-8")
    assert [line for line in previous.splitlines() if line.startswith("|")] == [
        line for line in rendered.splitlines() if line.startswith("|")
    ], "A table changed during a wording-only refresh"
    assert "checked again or skipped" not in rendered
    assert "Every listed trade and horizon exit is assumed to fill within" not in rendered
    assert "Price and cash ranges are estimates only, never execution limits." in rendered
    backup = REVIEW.with_name("Gameplan-2026-09-09-trade-review-before-estimate-clarification.md")
    assert not backup.exists(), "Review backup already exists"
    backup.write_bytes(REVIEW.read_bytes())
    REVIEW.write_text(rendered, encoding="utf-8")
    assert {str(path): file_checksum(path) for path in native_paths} == native_hashes
    evidence = {
        "status": "VERIFIED", "change": "Estimate-only execution semantics; all existing numerical tables preserved",
        "review_path": REVIEW.as_posix(), "review_sha256": file_checksum(REVIEW),
        "previous_review_sha256": EXPECTED, "previous_review_path": backup.as_posix(),
        "native_sources_preserved": native_hashes,
        "metadata_overrides": {"direction_based_projection.assumptions": ASSUMPTION,
                               "planning_price_path.market_gap_policy": PATH_POLICY},
        "orders_placed": 0, "broker_snapshot_refreshed": False,
    }
    (ROOT / "artifacts/analysis/gameplan-cash-ledger/estimate-semantics-review.json").write_text(
        json.dumps(evidence, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({"status": evidence["status"], "review_sha256": evidence["review_sha256"]}))


if __name__ == "__main__":
    main()
