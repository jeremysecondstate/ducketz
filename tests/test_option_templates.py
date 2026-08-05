from __future__ import annotations

from app.models.option_management import SavedExitPlanTemplate, SavedRollTemplate
from app.services.option_exit_plans import SINGLE_TARGET
from app.services.schwab_strategy_orders import GOOD_UNTIL_CANCELED
from app.ui.option_templates import option_template_workspace_rows


def test_template_workspace_shows_every_builtin_capability_and_safe_saved_defaults() -> None:
    rows = option_template_workspace_rows(
        roll_templates=(
            SavedRollTemplate(
                name="Next monthly",
                days_forward=30,
                keep_strike_widths=True,
                duration=GOOD_UNTIL_CANCELED,
                price_policy="MID",
            ),
        ),
        exit_templates=(
            SavedExitPlanTemplate(
                name="Take 20",
                base_template_id=SINGLE_TARGET,
                target_percent=20,
                stop_percent=10,
                limit_offset=0.05,
                duration=GOOD_UNTIL_CANCELED,
            ),
        ),
    )

    by_name = {row.name: row for row in rows}
    assert {"Target + Stop", "Single Target", "2 Targets", "Trailing Stop"} <= set(by_name)
    assert "Placeable" in by_name["Single Target"].availability
    assert "Review only" in by_name["Target + Stop"].availability
    assert "Unavailable" in by_name["2 Targets"].availability
    assert "Unavailable" in by_name["Trailing Stop"].availability
    assert by_name["Next monthly"].category == "Saved Roll"
    assert by_name["Take 20"].category == "Saved Exit"
    combined = " ".join(value for row in rows for value in row.__dict__.values()).casefold()
    assert "account" not in combined
    assert "occ" not in combined
