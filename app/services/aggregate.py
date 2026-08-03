from __future__ import annotations

from dataclasses import dataclass

from app.models.portfolio import PortfolioSnapshot


@dataclass(frozen=True)
class DucketBucketSnapshot:
    snapshots: list[PortfolioSnapshot]

    @property
    def cash_value(self) -> float:
        return round(sum(snapshot.cash_value for snapshot in self.snapshots), 2)

    @property
    def holdings_value(self) -> float:
        return round(sum(snapshot.holdings_value for snapshot in self.snapshots), 2)

    @property
    def total_value(self) -> float:
        return round(sum(snapshot.total_value for snapshot in self.snapshots), 2)

    @property
    def unrealized_pnl(self) -> float | None:
        values = [snapshot.unrealized_pnl for snapshot in self.snapshots if snapshot.unrealized_pnl is not None]
        return round(sum(values), 2) if values else None

    @property
    def day_pnl(self) -> float | None:
        values = [snapshot.day_pnl for snapshot in self.snapshots if snapshot.day_pnl is not None]
        return round(sum(values), 2) if values else None

    @property
    def day_pnl_accounts(self) -> list[str]:
        return [
            snapshot.account_label
            for snapshot in self.snapshots
            if snapshot.day_pnl is not None
        ]