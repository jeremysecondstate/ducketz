from __future__ import annotations

import pytest

from app.main import _schwab_authorization_code


REDIRECT = "https://secondstate.art/schwab/callback"


def test_schwab_authorization_accepts_matching_callback_url() -> None:
    assert (
        _schwab_authorization_code(
            "https://secondstate.art/schwab/callback?code=abc_def%40&state=current",
            expected_state="current",
            expected_redirect_uri=REDIRECT,
        )
        == "abc_def@"
    )


def test_schwab_authorization_accepts_minimal_flow_callback() -> None:
    assert (
        _schwab_authorization_code(
            "https://secondstate.art/schwab/callback?code=abc_def%40",
            expected_state=None,
            expected_redirect_uri=REDIRECT,
        )
        == "abc_def@"
    )


def test_schwab_authorization_keeps_direct_code_compatibility() -> None:
    assert (
        _schwab_authorization_code(
            "abc_def@",
            expected_state="current",
            expected_redirect_uri=REDIRECT,
        )
        == "abc_def@"
    )


@pytest.mark.parametrize(
    "callback",
    (
        "https://secondstate.art/schwab/callback?code=abc&state=old",
        "https://attacker.invalid/schwab/callback?code=abc&state=current",
        "https://secondstate.art/schwab/callback?state=current",
        "https://secondstate.art/schwab/callback?code=abc&code=def&state=current",
    ),
)
def test_schwab_authorization_rejects_unbound_callback(callback: str) -> None:
    with pytest.raises(ValueError):
        _schwab_authorization_code(
            callback,
            expected_state="current",
            expected_redirect_uri=REDIRECT,
        )
