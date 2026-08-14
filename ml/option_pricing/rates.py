from __future__ import annotations

import json
import math
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Mapping, Sequence

import exchange_calendars as xcals
import pandas as pd

from ml.artifacts import file_checksum, utc_timestamp


FMP_TREASURY_CURVE_SCHEMA_VERSION = "fmp-treasury-curve-v1"
TREASURY_RATE_RESOLUTION_POLICY_VERSION = (
    "causal-log-discount-maturity-matched-v1"
)
FMP_TREASURY_NODE_YEARS: Mapping[str, float] = {
    "month1": 1.0 / 12.0,
    "month2": 2.0 / 12.0,
    "month3": 3.0 / 12.0,
    "month6": 6.0 / 12.0,
    "year1": 1.0,
    "year2": 2.0,
    "year3": 3.0,
    "year5": 5.0,
    "year7": 7.0,
    "year10": 10.0,
    "year20": 20.0,
    "year30": 30.0,
}


@dataclass(frozen=True)
class RateResolution:
    rate: float
    source: str
    observation_date: str | None
    source_available_at: pd.Timestamp
    maturity_years: float
    lower_node_years: float | None
    upper_node_years: float | None
    policy_version: str = TREASURY_RATE_RESOLUTION_POLICY_VERSION


def publish_fmp_treasury_curve(
    datastore_root: Path,
    raw_response: object,
    *,
    received_at: object,
) -> Path:
    """Commit an already-fetched FMP response without making a provider call."""

    received = utc_timestamp(received_at)
    records = _response_records(raw_response)
    normalized = _normalize_fmp_treasury_records(records, received_at=received)
    if normalized.empty:
        raise ValueError("FMP Treasury response contains no usable maturity nodes")
    authority = (
        Path(datastore_root).resolve()
        / "pools"
        / "rates"
        / "treasury-curve"
        / "fmp"
    )
    authority.mkdir(parents=True, exist_ok=True)
    name = received.strftime("%Y%m%dT%H%M%S.%fZ")
    destination = authority / name
    if destination.exists():
        verified = read_fmp_treasury_curve(destination, datastore_root=datastore_root)
        expected = normalized.sort_values(
            ["observation_date", "maturity_years"], kind="stable"
        ).reset_index(drop=True)
        observed = verified.sort_values(
            ["observation_date", "maturity_years"], kind="stable"
        ).reset_index(drop=True)
        if observed.equals(expected):
            return destination
        raise RuntimeError("Divergent FMP Treasury evidence has the same receipt identity")
    staging = Path(tempfile.mkdtemp(prefix=f".{name}.tmp-{os.getpid()}-", dir=authority))
    try:
        raw_path = staging / "raw-response.json"
        raw_path.write_text(
            json.dumps(raw_response, indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8",
        )
        nodes_path = staging / "normalized-nodes.parquet"
        normalized.to_parquet(nodes_path, index=False)
        manifest = {
            "schema_version": FMP_TREASURY_CURVE_SCHEMA_VERSION,
            "provider": "fmp",
            "endpoint": "stable/treasury-rates",
            "received_at": received.isoformat(),
            "observation_dates": sorted(
                normalized["observation_date"].astype(str).unique().tolist()
            ),
            "outputs": _inventory(staging, (raw_path.name, nodes_path.name)),
        }
        manifest_path = staging / "manifest.json"
        _write_json(manifest_path, manifest)
        _write_json(
            staging / "receipt.json",
            {
                "schema_version": FMP_TREASURY_CURVE_SCHEMA_VERSION,
                "received_at": received.isoformat(),
                "run_path": destination.relative_to(Path(datastore_root).resolve()).as_posix(),
                "manifest_checksum_sha256": file_checksum(manifest_path),
                "automated_action_allowed": False,
            },
        )
        staging.replace(destination)
    except BaseException:
        # An incomplete staging directory has no authority pointer and remains invisible.
        raise
    read_fmp_treasury_curve(destination, datastore_root=datastore_root)
    return destination


def read_fmp_treasury_curve(
    directory: Path,
    *,
    datastore_root: Path,
) -> pd.DataFrame:
    root = Path(datastore_root).resolve()
    run = Path(directory).resolve()
    authority = root / "pools" / "rates" / "treasury-curve" / "fmp"
    if run.parent != authority:
        raise RuntimeError("FMP Treasury run escapes its immutable authority")
    manifest_path = run / "manifest.json"
    receipt_path = run / "receipt.json"
    manifest = _read_json(manifest_path)
    receipt = _read_json(receipt_path)
    if (
        manifest.get("schema_version") != FMP_TREASURY_CURVE_SCHEMA_VERSION
        or receipt.get("schema_version") != FMP_TREASURY_CURVE_SCHEMA_VERSION
        or receipt.get("manifest_checksum_sha256") != file_checksum(manifest_path)
        or receipt.get("run_path") != run.relative_to(root).as_posix()
    ):
        raise RuntimeError("FMP Treasury receipt does not match its immutable run")
    for name, metadata in manifest.get("outputs", {}).items():
        path = run / str(name)
        if (
            not path.is_file()
            or int(metadata.get("size", -1)) != path.stat().st_size
            or metadata.get("checksum_sha256") != file_checksum(path)
        ):
            raise RuntimeError(f"FMP Treasury output failed verification: {path}")
    frame = pd.read_parquet(run / "normalized-nodes.parquet")
    frame["source_available_at"] = pd.to_datetime(
        frame["source_available_at"], utc=True, errors="coerce"
    )
    frame["observation_date"] = pd.to_datetime(
        frame["observation_date"], errors="coerce"
    ).dt.normalize()
    return frame


def load_verified_fmp_treasury_curves(datastore_root: Path) -> pd.DataFrame:
    root = Path(datastore_root).resolve()
    authority = root / "pools" / "rates" / "treasury-curve" / "fmp"
    frames: list[pd.DataFrame] = []
    for receipt in sorted(authority.glob("*/receipt.json")):
        frames.append(read_fmp_treasury_curve(receipt.parent, datastore_root=root))
    return pd.concat(frames, ignore_index=True, sort=False) if frames else pd.DataFrame()


def resolve_rate_for_expiration(
    as_of: object,
    expiration: object,
    *,
    datastore_root: Path | None = None,
    curve_nodes: pd.DataFrame | None = None,
    fallback_observations: pd.DataFrame | None = None,
) -> RateResolution:
    """Resolve a receipt-proven, maturity-matched continuous rate causally."""

    decision = utc_timestamp(as_of)
    expiry = utc_timestamp(expiration)
    maturity = (expiry - decision).total_seconds() / (365.0 * 24.0 * 3600.0)
    if maturity <= 0.0:
        raise ValueError("Expiration must be later than as_of")
    nodes = (
        curve_nodes.copy()
        if curve_nodes is not None
        else load_verified_fmp_treasury_curves(Path(datastore_root))
        if datastore_root is not None
        else pd.DataFrame()
    )
    if not nodes.empty:
        nodes["source_available_at"] = pd.to_datetime(
            nodes["source_available_at"], utc=True, errors="coerce"
        )
        nodes["observation_date"] = pd.to_datetime(
            nodes["observation_date"], errors="coerce"
        ).dt.normalize()
        fully_received = nodes["source_available_at"].lt(decision)
        observation_dates = nodes["observation_date"].dt.date
        # During the regular session, a same-day daily curve is not presumed
        # complete even if an early receipt exists. Outside that session, a
        # verified same-day curve may be selected after its full receipt.
        causal_date = (
            observation_dates.lt(decision.date())
            if _during_xnys_regular_session(decision)
            else observation_dates.le(decision.date())
        )
        eligible = nodes.loc[fully_received & causal_date].copy()
        if not eligible.empty:
            observation = eligible["observation_date"].max()
            same_day = eligible.loc[eligible["observation_date"].eq(observation)]
            receipt = same_day["source_available_at"].max()
            curve = same_day.loc[same_day["source_available_at"].eq(receipt)].copy()
            curve["maturity_years"] = pd.to_numeric(
                curve["maturity_years"], errors="coerce"
            )
            curve["continuous_rate"] = pd.to_numeric(
                curve["continuous_rate"], errors="coerce"
            )
            curve = curve.dropna(subset=["maturity_years", "continuous_rate"])
            curve = curve.loc[curve["maturity_years"].gt(0.0)].sort_values(
                "maturity_years", kind="stable"
            )
            if not curve.empty:
                rate, lower, upper = _interpolate_log_discount(curve, maturity)
                return RateResolution(
                    rate=rate,
                    source="FMP_TREASURY_CURVE",
                    observation_date=pd.Timestamp(observation).date().isoformat(),
                    source_available_at=pd.Timestamp(receipt),
                    maturity_years=maturity,
                    lower_node_years=lower,
                    upper_node_years=upper,
                )
    if fallback_observations is not None and not fallback_observations.empty:
        fallback = fallback_observations.copy()
        fallback["available_at"] = pd.to_datetime(
            fallback["available_at"], utc=True, errors="coerce"
        )
        fallback["risk_free_rate"] = pd.to_numeric(
            fallback["risk_free_rate"], errors="coerce"
        )
        fallback = fallback.loc[
            fallback["available_at"].lt(decision)
            & fallback["risk_free_rate"].between(-0.20, 1.0)
        ].sort_values("available_at")
        if not fallback.empty:
            row = fallback.iloc[-1]
            return RateResolution(
                rate=float(row["risk_free_rate"]),
                source="ALFRED_FEDFUNDS_FALLBACK",
                observation_date=None,
                source_available_at=pd.Timestamp(row["available_at"]),
                maturity_years=maturity,
                lower_node_years=None,
                upper_node_years=None,
            )
    raise LookupError("No causal Treasury curve or ALFRED/FRED fallback was available")


def _interpolate_log_discount(
    curve: pd.DataFrame, maturity: float
) -> tuple[float, float, float]:
    years = curve["maturity_years"].to_numpy(dtype=float)
    rates = curve["continuous_rate"].to_numpy(dtype=float)
    log_discounts = -rates * years
    if maturity <= years[0]:
        lower, upper = 0.0, float(years[0])
        log_discount = float(log_discounts[0]) * maturity / years[0]
    elif maturity >= years[-1]:
        lower = upper = float(years[-1])
        log_discount = -float(rates[-1]) * maturity
    else:
        upper_index = int((years >= maturity).argmax())
        lower_index = upper_index - 1
        lower, upper = float(years[lower_index]), float(years[upper_index])
        fraction = (maturity - lower) / (upper - lower)
        log_discount = float(
            log_discounts[lower_index]
            + fraction * (log_discounts[upper_index] - log_discounts[lower_index])
        )
    return -log_discount / maturity, lower, upper


def _during_xnys_regular_session(value: pd.Timestamp) -> bool:
    decision = utc_timestamp(value)
    local_date = decision.tz_convert("America/New_York").date()
    calendar = xcals.get_calendar("XNYS")
    session = pd.Timestamp(local_date)
    if session not in calendar.sessions:
        return False
    opened = pd.Timestamp(calendar.session_open(session)).tz_convert("UTC")
    closed = pd.Timestamp(calendar.session_close(session)).tz_convert("UTC")
    return bool(opened <= decision <= closed)


def _normalize_fmp_treasury_records(
    records: Sequence[Mapping[str, object]], *, received_at: pd.Timestamp
) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for record in records:
        observation = pd.to_datetime(record.get("date"), errors="coerce")
        if pd.isna(observation):
            continue
        for field, years in FMP_TREASURY_NODE_YEARS.items():
            value = pd.to_numeric(record.get(field), errors="coerce")
            if pd.isna(value) or not math.isfinite(float(value)):
                continue
            rows.append(
                {
                    "observation_date": pd.Timestamp(observation).normalize(),
                    "source_available_at": received_at,
                    "node_name": field,
                    "maturity_years": float(years),
                    "quoted_percent": float(value),
                    "continuous_rate": float(value) / 100.0,
                    "provider": "fmp",
                    "schema_version": FMP_TREASURY_CURVE_SCHEMA_VERSION,
                }
            )
    return pd.DataFrame(rows)


def _response_records(raw_response: object) -> list[Mapping[str, object]]:
    value = raw_response
    if isinstance(value, Mapping):
        value = value.get("data", value.get("results", [value]))
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise TypeError("Provider response must be a JSON object or list")
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _inventory(directory: Path, names: Sequence[str]) -> dict[str, dict[str, object]]:
    return {
        name: {
            "size": (directory / name).stat().st_size,
            "checksum_sha256": file_checksum(directory / name),
        }
        for name in names
    }


def _read_json(path: Path) -> Mapping[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Authority metadata is unreadable: {path}") from exc
    if not isinstance(value, Mapping):
        raise RuntimeError(f"Authority metadata is malformed: {path}")
    return value


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.write_text(
        json.dumps(dict(payload), indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )


def load_point_in_time_rate_observations(
    datastore_root: Path,
) -> tuple[pd.DataFrame | None, tuple[Path, ...]]:
    """Load causal FRED rate releases without inventing a fallback value."""

    root = Path(datastore_root).resolve()
    paths = tuple(
        path
        for context_name in (
            "alfred-release-context",
            "prospective-release-context",
            "release-context",
        )
        for path in sorted(
            (
                root
                / "pools"
                / "macro"
                / "features"
                / context_name
                / "fred"
            ).glob("*.parquet")
        )
    )
    if not paths:
        return None, ()
    frames = [pd.read_parquet(path) for path in paths]
    combined = pd.concat(frames, ignore_index=True, sort=False)
    required = {"fed_funds_available_at", "macro__fed_funds_level"}
    if not required.issubset(combined.columns):
        return None, paths
    output = pd.DataFrame(
        {
            "available_at": pd.to_datetime(
                combined["fed_funds_available_at"], utc=True, errors="coerce"
            ),
            # FRED FEDFUNDS is quoted in percentage points.
            "risk_free_rate": pd.to_numeric(
                combined["macro__fed_funds_level"], errors="coerce"
            )
            / 100.0,
        }
    ).dropna()
    output = output.loc[output["risk_free_rate"].between(-0.20, 1.0)]
    output = output.sort_values("available_at").drop_duplicates(
        "available_at", keep="last"
    )
    return (output.reset_index(drop=True) if not output.empty else None), paths


def rate_coverage_report(
    observations: pd.DataFrame | None,
    *,
    target_snapshot_fors: Sequence[object],
    source_backward_minutes: int = 5,
) -> dict[str, object]:
    """Prove a strictly prior rate exists at every planned source boundary."""

    targets = sorted({utc_timestamp(value) for value in target_snapshot_fors})
    if observations is None or observations.empty:
        covered: list[pd.Timestamp] = []
        available = pd.Series(dtype="datetime64[ns, UTC]")
    else:
        available = pd.to_datetime(
            observations.get("available_at"), utc=True, errors="coerce"
        ).dropna().sort_values()
        covered = [
            target
            for target in targets
            if available.lt(
                target - pd.Timedelta(minutes=source_backward_minutes)
            ).any()
        ]
    missing = sorted(set(targets).difference(covered))
    return {
        "status": "PASS" if targets and not missing else "NOT_PROVEN",
        "target_count": len(targets),
        "covered_target_count": len(covered),
        "rate_observation_count": len(available),
        "source_backward_minutes": source_backward_minutes,
        "first_rate_available_at": (
            pd.Timestamp(available.iloc[0]).isoformat() if len(available) else None
        ),
        "last_rate_available_at": (
            pd.Timestamp(available.iloc[-1]).isoformat() if len(available) else None
        ),
        "missing_targets": [value.isoformat() for value in missing],
    }


__all__ = [
    "FMP_TREASURY_CURVE_SCHEMA_VERSION",
    "FMP_TREASURY_NODE_YEARS",
    "RateResolution",
    "TREASURY_RATE_RESOLUTION_POLICY_VERSION",
    "load_point_in_time_rate_observations",
    "load_verified_fmp_treasury_curves",
    "publish_fmp_treasury_curve",
    "rate_coverage_report",
    "read_fmp_treasury_curve",
    "resolve_rate_for_expiration",
]
