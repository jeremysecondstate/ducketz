"""One explicit Atlas quarter-hour offline cycle; no scheduler or live entrypoint."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
import json
from pathlib import Path

from .prediction import ContractError, PACIFIC, PredictionHistory, STEP, TARGET_VERSION, instant, issue_prediction, sizing_features
from .sizing import size


@dataclass(frozen=True)
class AtlasScope:
    atlas_symbols: tuple[str, ...]
    gameplan_symbols: tuple[str, ...]

    @classmethod
    def from_local_files(cls, *, profile: Path, watchlist: Path, ownership: Path):
        """Read only symbol/executor bindings; never copy credentials or activate."""
        local = json.loads(Path(profile).read_text(encoding="utf-8"))
        account = json.loads(Path(ownership).read_text(encoding="utf-8"))
        watch = tuple(line.strip() for line in Path(watchlist).read_text(encoding="utf-8").splitlines()
                      if line.strip() and not line.lstrip().startswith("#"))
        own = tuple(account["participants"]["pc-original"])
        peer = tuple(account["participants"]["pc-new"])
        if (local["actor"] != "Atlas" or local["machine"] != "pc-original"
                or account["machine_id"] != "pc-original" or account["coordinator_id"] != "pc-original"
                or len(set(watch)) != len(watch) or len(set(own)) != len(own) or len(set(peer)) != len(peer)
                or set(own) & set(peer) or set(watch) != set(own) or set(local["symbols"]) != set(own)):
            raise ContractError("Atlas profile/watchlist/ownership disagree; preserve operating bindings")
        return cls(tuple(sorted(own)), tuple(sorted(own + peer)))


class AtlasPrototype:
    def __init__(self, *, scope: AtlasScope, history: PredictionHistory):
        self.scope, self.history = scope, history

    def issue_cycle(self, candles, *, issued_at, eligible_session: date, model_version,
                    model_target_version, infer_by_symbol):
        """Full local workload, one immutable issue per symbol/quarter-hour.

        On a partial-cycle retry, reuse stored issues before running inference.
        Models are supplied explicitly; no fitted model, acquisition or training
        is invoked. Failed symbols remain visible instead of inventing predictions.
        """
        now = instant(issued_at)
        local = now.astimezone(PACIFIC)
        if local.date() != eligible_session or model_target_version != TARGET_VERSION:
            raise ContractError("Cycle requires the eligible session and explicit not-down model target")
        slot = local.replace(minute=local.minute // 15 * 15, second=0, microsecond=0)
        reference = (instant(slot) - STEP).isoformat()
        result = {"predictions": {}, "failures": {}}
        for symbol in self.scope.atlas_symbols:
            saved = self.history.for_slot(symbol, reference)
            if saved:
                result["predictions"][symbol] = saved
                continue
            try:
                prediction = issue_prediction(candles, symbol=symbol, issued_at=issued_at,
                    eligible_session=eligible_session, model_version=model_version,
                    model_target_version=model_target_version, infer=infer_by_symbol[symbol])
                result["predictions"][symbol] = self.history.save(prediction)
            except (ContractError, KeyError) as exc:
                result["failures"][symbol] = str(exc)
        return result

    def explain_size(self, prediction, candles, *, side, expected_volume, holdings, recent_executed,
                     hourly_reference, reference_version, score_mapping, score_version,
                     half_share_ties=None, rounded_equality=None):
        if prediction.symbol not in self.scope.atlas_symbols:
            raise ContractError("Prediction outside Atlas intraday ownership")
        original = self.history.get(prediction.prediction_id)
        if original != prediction:
            raise ContractError("Sizing must use an original immutable issue")
        # Freeze sizing to the recorded candle inputs, rather than later revisions.
        frozen = json.loads(prediction.inputs_json)["candles"]
        from .prediction import causal_candles, encoded
        relevant = causal_candles(candles, symbol=prediction.symbol, issued_at=prediction.issued_at)
        if encoded([asdict(c) for c in relevant]) != encoded(frozen):
            raise ContractError("Sizing candle inputs differ from original prediction inputs")
        inputs = sizing_features(candles, symbol=prediction.symbol, issued_at=prediction.issued_at,
                                 expected_volume=expected_volume, holdings=holdings, recent_executed=recent_executed)
        inputs["probability_not_down"] = prediction.probability_not_down
        return size(symbol=prediction.symbol, side=side, hourly_reference=hourly_reference,
                    reference_version=reference_version, score=score_mapping(dict(inputs)), score_version=score_version,
                    inputs=inputs, half_share_ties=half_share_ties, rounded_equality=rounded_equality)

    def instruction(self, prediction, explanation, *, request_id, allocation_caps=(), sale_policy_version=None):
        """Bind explicit fixture direction/sizing to the preserved issue.

        A zero nearest-share result naturally has no executable quantity; this
        introduces neither a one-share minimum nor an additional trading veto.
        The zero limit is a placeholder; OfflineExecutor obtains the actual quote.
        """
        from .book import Request
        if self.history.get(prediction.prediction_id) != prediction or explanation.symbol != prediction.symbol:
            raise ContractError("Instruction differs from issued prediction or sizing symbol")
        if prediction.symbol not in self.scope.atlas_symbols:
            raise ContractError("Instruction outside Atlas intraday ownership")
        if explanation.desired_quantity == 0:
            return None
        return Request(request_id, prediction.prediction_id, prediction.symbol, explanation.side,
                       explanation.desired_quantity, "0", "atlas-15m", allocation_caps=allocation_caps,
                       sale_policy_version=sale_policy_version, sizing=explanation.payload())
