from __future__ import annotations

import sys
from urllib.parse import parse_qs, urlparse


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "schwab-auth":
        from app.services.schwab import SchwabSession

        session = SchwabSession()
        authorization_url, state = session.build_authorization_url()
        print("Open this URL and approve access:")
        print(authorization_url)
        response = input(
            "Paste the complete Schwab redirect URL (preferred), or the authorization code: "
        )
        code = _schwab_authorization_code(
            response,
            expected_state=state,
            expected_redirect_uri=session.config.redirect_uri,
        )
        session.exchange_authorization_code(code)
        print("Schwab authorization saved.")
        return

    if len(sys.argv) > 1 and sys.argv[1] == "ui":
        from app.ui.ducket_bucket import run_ducket_bucket_ui

        run_ducket_bucket_ui()
        return

    from app.services.aggregate import DucketBucketSnapshot
    from app.services.hyperliquid import sync_hyperliquid_portfolios
    from app.services.schwab import sync_schwab_portfolio

    bucket = DucketBucketSnapshot(
        snapshots=[sync_schwab_portfolio(), *sync_hyperliquid_portfolios()]
    )

    print("DUCKET BUCKET")
    print("=============")
    print(f"Cash: ${bucket.cash_value:,.2f}")
    print(f"Holdings: ${bucket.holdings_value:,.2f}")
    print(f"Total: ${bucket.total_value:,.2f}")
    print(f"Unrealized PnL: {_money_or_dash(bucket.unrealized_pnl)}")
    print(f"Day PnL: {_money_or_dash(bucket.day_pnl)} ({_coverage_or_dash(bucket.day_pnl_accounts)})")

    for snapshot in bucket.snapshots:
        print()
        print(snapshot.account_label.upper())
        print("-" * len(snapshot.account_label))
        print(f"Status: {snapshot.status}")
        print(f"Cash: ${snapshot.cash_value:,.2f}")
        print(f"Holdings: ${snapshot.holdings_value:,.2f}")
        print(f"Total: ${snapshot.total_value:,.2f}")
        print(f"Unrealized PnL: {_money_or_dash(snapshot.unrealized_pnl)}")
        print(f"Day PnL: {_money_or_dash(snapshot.day_pnl)}")

        print()
        print("Cash")
        for cash in snapshot.cash:
            print(f"- {cash.bucket} {cash.symbol}: {cash.amount:g} = ${cash.value:,.2f}")

        print()
        print("Holdings")
        for holding in snapshot.holdings:
            print(
                f"- {holding.bucket} {holding.symbol}: "
                f"{holding.quantity:g} @ ${holding.price:,.4f} = ${holding.value:,.2f}, "
                f"uPnL {_money_or_dash(holding.unrealized_pnl)}, "
                f"day {_money_or_dash(holding.day_pnl)}"
            )


def _money_or_dash(value: float | None) -> str:
    return "--" if value is None else f"${value:,.2f}"


def _coverage_or_dash(labels: list[str]) -> str:
    return " + ".join(labels) if labels else "no account day PnL available"


def _schwab_authorization_code(
    value: str,
    *,
    expected_state: str | None,
    expected_redirect_uri: str,
) -> str:
    """Extract a code while binding a pasted callback to this auth attempt."""

    raw = str(value).strip()
    if not raw:
        raise ValueError("Schwab authorization response is required.")
    parsed = urlparse(raw)
    if not parsed.scheme and not parsed.netloc:
        return raw
    expected = urlparse(str(expected_redirect_uri).strip())
    observed_redirect = (
        parsed.scheme.casefold(),
        parsed.netloc.casefold(),
        parsed.path.rstrip("/"),
    )
    configured_redirect = (
        expected.scheme.casefold(),
        expected.netloc.casefold(),
        expected.path.rstrip("/"),
    )
    if observed_redirect != configured_redirect:
        raise ValueError("Schwab redirect URL does not match the configured callback.")
    query = parse_qs(parsed.query, keep_blank_values=True)
    codes = [str(item).strip() for item in query.get("code", [])]
    if len(codes) != 1 or not codes[0]:
        raise ValueError("Schwab redirect URL must contain exactly one authorization code.")
    # Schwab's current documented authorize request does not carry state.  If
    # support for state is reintroduced, fail closed unless the callback binds
    # to the exact value generated for this attempt.
    if expected_state is not None:
        import hmac

        states = [str(item).strip() for item in query.get("state", [])]
        if (
            len(states) != 1
            or not states[0]
            or not hmac.compare_digest(states[0], expected_state)
        ):
            raise ValueError("Schwab redirect state does not match this authorization attempt.")
    return codes[0]


if __name__ == "__main__":
    main()
