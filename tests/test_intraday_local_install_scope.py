"""Local-trader instructions use the existing exact application install scope."""
import pytest
from ml.nightly_exchange_repair import _application_path


def test_local_trader_runbook_install_path_is_explicit():
    _application_path("docs/development/scout-local-15m-manual-start.md")
    with pytest.raises(ValueError,match="scope"):
        _application_path("docs/development/scout-local-other.md")
