"""Local-trader instructions use the existing exact application install scope."""
import pytest
from ml.nightly_exchange_repair import _application_path


def test_local_trader_runbook_install_path_is_explicit():
    _application_path("docs/development/scout-local-15m-manual-start.md")
    with pytest.raises(ValueError,match="scope"):
        _application_path("docs/development/scout-local-other.md")


def test_standing_nightly_workflow_can_be_installed_without_unrelated_scope():
    for name in ("ml/joint_capital_plan.py", "ml/nightly_synthesis.py", "ml/joint_capital_adoption.py",
                 "app/ui/gameplan_data.py", "tools/nightly_exchange.py",
                 "ml/stock_trader/gameplan_execution.py", "ml/stock_trader/independent_runtime.py",
                 "tests/test_nightly_plan_only.py", "AGENTS.md", "docs/development/nightly-exchange.md"):
        _application_path(name)
    with pytest.raises(ValueError,match="scope"):
        _application_path("app/main.py")
