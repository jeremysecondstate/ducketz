from __future__ import annotations

from pathlib import Path
from typing import Mapping, Sequence

import pandas as pd

from datafetching.decision_time import latest_completed_bar_clock
from ml.option_pricing.causal import build_causal_samples, completed_bar_close
from ml.option_pricing.opra import (
    OPRA_RECEIPT_NAME,
    normalize_cbbo_records,
    normalize_definition_records,
    point_in_time_definition_asof,
    read_opra_import,
    select_historical_source_target,
)
from ml.option_pricing.policies import ContractSelectionPolicy


def materialize_committed_opra_history(
    datastore_root: Path,
    *,
    symbols: Sequence[str],
    rate_observations: pd.DataFrame | None,
    contract_policy: ContractSelectionPolicy | None = None,
) -> tuple[pd.DataFrame, tuple[Path, ...], Mapping[str, str]]:
    """Materialize verified OPRA evidence as explicitly OFFLINE causal rows."""

    root = Path(datastore_root).resolve()
    evidence_root = root / "ml" / "option-pricing-evidence" / "opra"
    clean_symbols = {
        str(value).strip().upper() for value in symbols if str(value).strip()
    }
    if not evidence_root.is_dir():
        return pd.DataFrame(), (), {}
    definitions: list[pd.DataFrame] = []
    cbbo_imports: list[tuple[Path, Mapping[str, object]]] = []
    source_files: list[Path] = []
    errors: dict[str, str] = {}
    for receipt_path in sorted(evidence_root.glob(f"*/{OPRA_RECEIPT_NAME}")):
        try:
            verified = read_opra_import(receipt_path.parent, datastore_root=root)
            manifest = verified["manifest"]
            phase = str(manifest.get("phase"))
            if phase == "definitions":
                for name in manifest.get("outputs", {}):
                    path = receipt_path.parent / str(name)
                    definitions.append(normalize_definition_records(_read_dbn(path)))
                    source_files.append(path)
            elif phase == "cbbo":
                cbbo_imports.append((receipt_path.parent, manifest))
            source_files.extend((receipt_path.parent / "manifest.json", receipt_path))
        except Exception as exc:
            errors[str(receipt_path.parent)] = f"{type(exc).__name__}: {exc}"
    if not definitions or not cbbo_imports:
        return pd.DataFrame(), tuple(dict.fromkeys(source_files)), errors
    definition_frame = pd.concat(definitions, ignore_index=True, sort=False)

    samples: list[pd.DataFrame] = []
    for directory, manifest in cbbo_imports:
        request_by_output = {
            str(request.get("output_name")): request
            for request in manifest.get("requests", [])
            if isinstance(request, Mapping)
        }
        for name in manifest.get("outputs", {}):
            request = request_by_output.get(str(name))
            route_name = f"{directory.name}/{name}"
            if request is None:
                errors[route_name] = "OpraImportError: CBBO output has no request metadata"
                continue
            parsed = _request_target(request)
            if parsed is None:
                errors[route_name] = "OpraImportError: CBBO request purpose has no target"
                continue
            symbol, target = parsed
            if symbol not in clean_symbols:
                continue
            path = directory / str(name)
            try:
                cbbo = normalize_cbbo_records(_read_dbn(path))
                source_quotes, target_quotes = select_historical_source_target(
                    cbbo,
                    target_snapshot_for=target,
                )
                if source_quotes.empty or target_quotes.empty:
                    raise ValueError("Required backward source or forward target CBBO is missing")
                source_time = pd.Timestamp(source_quotes["quote_timestamp"].max())
                target_observed_at = pd.Timestamp(target_quotes["quote_timestamp"].max())
                definitions_asof = point_in_time_definition_asof(
                    definition_frame.loc[
                        definition_frame["symbol"].astype("string").str.upper().eq(symbol)
                    ],
                    source_time,
                )
                if definitions_asof.empty:
                    raise ValueError("No point-in-time definition existed by source surface")
                source_clock = latest_completed_bar_clock(root, symbol=symbol, as_of=source_time)
                target_clock = latest_completed_bar_clock(root, symbol=symbol, as_of=target)
                if pd.Timestamp(target_clock.decision_timestamp) != target:
                    raise ValueError("No exact completed underlying bar at OPRA target")
                source_underlying = completed_bar_close(source_clock)
                target_underlying = completed_bar_close(target_clock)
                source_contracts = _opra_contract_frame(
                    source_quotes,
                    definitions_asof,
                    underlying_price=source_underlying,
                    target_snapshot_for=target,
                )
                target_contracts = _opra_contract_frame(
                    target_quotes,
                    definitions_asof,
                    underlying_price=target_underlying,
                    target_snapshot_for=target,
                )
                frame = build_causal_samples(
                    source_contracts,
                    target_contracts=target_contracts,
                    target_underlying_price=target_underlying,
                    source_snapshot_for=source_time,
                    source_available_at=source_time,
                    target_snapshot_for=target,
                    source_provider="databento-opra",
                    prediction_mode="OFFLINE",
                    observed_available_at=target_observed_at,
                    contract_policy=contract_policy,
                    rate_observations=rate_observations,
                )
                samples.append(frame)
                source_files.extend((path, source_clock.source_file, target_clock.source_file))
            except Exception as exc:
                errors[route_name] = f"{type(exc).__name__}: {exc}"
    return (
        pd.concat(samples, ignore_index=True, sort=False) if samples else pd.DataFrame(),
        tuple(dict.fromkeys(source_files)),
        errors,
    )


def _opra_contract_frame(
    quotes: pd.DataFrame,
    definitions: pd.DataFrame,
    *,
    underlying_price: float,
    target_snapshot_for: pd.Timestamp,
) -> pd.DataFrame:
    merged = quotes.merge(
        definitions,
        on="contract_symbol",
        how="inner",
        validate="one_to_one",
        suffixes=("", "_definition"),
    )
    output = pd.DataFrame(
        {
            "symbol": merged["symbol"].astype("string").str.upper(),
            "contract_symbol": merged["contract_symbol"],
            "call_put": merged["call_put"].astype("string").str.upper(),
            "expiration_date": merged["expiration_date"],
            "strike": merged["strike"],
            "underlying_price": float(underlying_price),
            "bid": merged["bid"],
            "ask": merged["ask"],
            "multiplier": merged["multiplier"],
            "mini": False,
            "non_standard": ~merged["standard_contract"].fillna(False).astype(bool),
            "interest_rate": float("nan"),
            "dividend_yield": float("nan"),
            "implied_volatility": float("nan"),
            "quote_timestamp": merged["quote_timestamp"],
            "quote_staleness_seconds": (
                target_snapshot_for
                - pd.to_datetime(merged["quote_timestamp"], utc=True, errors="coerce")
            ).dt.total_seconds().clip(lower=0),
        }
    )
    return output


def _request_target(request: Mapping[str, object]) -> tuple[str, pd.Timestamp] | None:
    purpose = str(request.get("purpose") or "")
    parts = purpose.split(":", 2)
    if len(parts) != 3 or parts[0] != "SOURCE_BACKWARD_TARGET_FORWARD":
        return None
    timestamp = pd.to_datetime(parts[2], utc=True, errors="coerce")
    if pd.isna(timestamp):
        return None
    return parts[1].strip().upper(), pd.Timestamp(timestamp)


def _read_dbn(path: Path) -> pd.DataFrame:
    import databento as db

    return db.DBNStore.from_file(path).to_df(
        price_type="fixed",
        pretty_ts=False,
        map_symbols=True,
    ).reset_index()


__all__ = ["materialize_committed_opra_history"]
