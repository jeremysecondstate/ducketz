from __future__ import annotations

import sys


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "schwab-auth":
        from app.services.schwab import SchwabSession

        session = SchwabSession()
        authorization_url, _state = session.build_authorization_url()
        print("Open this URL, log in, approve access, then copy the code from the redirect URL:")
        print(authorization_url)
        code = input("Schwab authorization code: ").strip()
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


if __name__ == "__main__":
    main()
