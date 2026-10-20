# Direction-only package compatibility repair

Actor: Atlas. Task: priority source reconciliation, October 20, 2026. Scope: shared reader correctness for Atlas and Scout; no symbol or local model tuning changes.

Scout PR268 source `837139da2f7ed8c4060ca5e2a0d9160d97e56de2`, Completion-Record `20261020T044328Z-e328fb6566d84637b868bed7a2c8a841`, publishes `direction-live-quote-v1` with no estimated price path. Atlas received the actual dated packet but required a legacy price-path hash and rejected the equivalent recorded `stock-direction-5001-4999-v3` identity. This repair retains the incoming bytes and both recorded policy names. Every current direction uses the original unrounded probability: BUY >=0.5001, SELL <=0.4999, HOLD strictly between. A missing model assessment or fitted history does not suppress a valid current direction.

The common source candidate contains the generic package reader/composer, pure instruction helper, current policy with the existing October19 prerequisite, and focused offline fixtures. The prerequisite retains existing policy work from Atlas source `eb6d2ae14561d6769ea9637705b36688c13d63fa` / Completion-Record `20261019T230408Z-aa9d97e17c0b4564b7f1a2201d2d91c2`. Verified publication base is `6792abd38873b3aab98db82b7f091525a49a428b`.

Optional installed Atlas adoption, nightly readiness and execution modules are absent from that main base. The exact local changes below carry their reviewed adaptation without recreating the removed architecture on main. Apply only to an installation that already owns the corresponding modules; review by hunk if surrounding source differs. These patches preserve concurrent local extensions. Main publication, local installation and loaded runtime are separate facts.

Current and mixed sources compose into instructions without predicted prices, quantities, fills or a simulated cash gate. Historical price-backed sources retain their arithmetic reader. Original labels and recorded policies remain source provenance. The local exporter verifies exact scientific fields before exporting current derivations; it does not manufacture a planning-price file or hash. Both displays accept nullable live quantities. The manual Gameplan executor reads current due forecasts and preserves them after its own broker capture rather than dropping them through an empty historical event ledger. Legacy catch-up, reservations, manual controls and independent trader boundaries remain intact. Existing nightly work continues independently of source publication.

## Exact local patch

Application baseline: `90704d21801f60426493bd6cc650a7fac38d4f61` plus the existing installed working composition. Initial/final hashes below bind those whole local files; the diff includes only this task's changes. New files have no initial hash. The patch uses LF; normalize a copy of each matching original to LF, apply the patch, then restore each listed final line ending. Isolated reconstruction verified every final raw SHA256. Preserve originals and existing publication ancestry. Local files and operating bindings outside this inventory are excluded.

The legacy synthetic execution fixture now explicitly supplies an empty working-order observation, preserving the existing production evidence guard. Final installed checks and isolated publication checks are recorded separately; no provider/broker verification or trader activation is implied.

| Path | Operation | Initial SHA256 | Final SHA256 | Final line endings |
| --- | --- | --- | --- | --- |
| `ml/joint_capital_plan.py` | modify | 16558224e284b141a7ebf0326b8fde4cd572756915ab37a615c64c9dce681f15 | b7347cb4cf83dd374ddb3a695d069621c70b0a614201c1e9e287891c67945cb6 | CRLF |
| `ml/stock_direction_policy.py` | modify | 4308d6e6e0a1119d4b62b8e2e71c67a91a250d81e555f254d231a882883abfef | 388b2100118408acb541f086e78274e4bbccf0550ac1cdfe2aad5bae64d76ae4 | LF |
| `ml/gameplan_execution_policy.py` | add | absent | e9d0776bdd7c7d34342911b26be2323b93ac82535c77313917b380bc84c9590a | LF |
| `tests/test_direction_only_joint_plan.py` | add | absent | dca8bc5106d1b731fa64a9162f5a1ee984572cce1142110896f16e25e0866c5b | LF |
| `ml/joint_capital_handoff.py` | modify | 8ca48c0104b291f784c1475d255280a4216102fd93b13464e6a839c9b90abd8f | 00d8b21a948d1307ebea7246b3af4820f192850328b05ddce223e85ec9d10fea | CRLF |
| `ml/joint_capital_adoption.py` | modify | f3f80088786cdd17a0f942a759ea0e53bcf49cfa4cab9e996002548b649ba4ba | 133b3c496f791a728b19bbb076b179a6972b77eae02c7be409d4abe8e18276e1 | LF |
| `app/ui/gameplan_data.py` | modify | 8f3b5a281a5d80037925609adaef0c86e79c16ea0193a604c06767c2943da30f | 8ea039f18e889bdb98bf610c5bc85db60741ec3e3376f04806dc23b52a3ac204 | CRLF |
| `ml/nightly_joint_readiness.py` | modify | 76d5a7bf81fed3cfe95c971d0a2439ba4f70be55aee200b565837db195c32aa0 | af3a44dea282edfc62d780143859776786f9214f01b6731c704d3cc388d509db | CRLF |
| `tests/test_direction_only_joint_adoption.py` | add | absent | d0f476c5daa528edbfe7b0630cdff361a51706c1cf022cc289c7d2a500db3644 | LF |
| `tests/test_direction_only_joint_handoff.py` | add | absent | b51822ed3a6bb43477d5d52b2ec5d02a86814505d80dd35d87c78150e4a8d923 | LF |
| `ml/stock_trader/gameplan_execution.py` | modify | be5faf7b1a23687b179ea5ded61b20c1d796f23c4223c2767e94b520f83b58b0 | 462773f6ab3d4d1b43b602eb15a75d7df27c9011bb91d5bc18a7efcfc88cdf70 | LF |
| `tests/test_direction_only_joint_execution.py` | add | absent | e1abf7067cdd699404db85b57d92ed9dc0a8622b24d3c94c63ea323e40a5ab79 | LF |
| `ml/stock_trader/independent_runtime.py` | modify | 7ef2c119fdb8e6bd989af7657c3c3bab74e120174103df4c373350e1463406ec | c7cc7402cc8ecfa73bd92d049b8bda8a26c409dc5b9b8afd435d80ea0951cbcf | CRLF |
| `tests/test_joint_capital_adoption.py` | modify | 89d9d9134b25805e9545b09f9d4a4c99b338453c7a56a34b79100889a1f91c47 | d0adb42e48dbf1f3eafefd15c8dfd04da2e3d6e44a1b874582cf25997c10d5b0 | LF |

```diff
--- a/ml/joint_capital_plan.py
+++ b/ml/joint_capital_plan.py
@@ -25,10 +25,13 @@
 from ml.gameplan_cash_ledger import project_direction_trades, SIGNAL_DRIVEN_HOLDING_POLICY
 from ml.stock_trader.contracts import StockTraderPolicy
 from ml.stock_trader.cross_horizon_fallback import validate_fallback_policy
+from ml.gameplan_execution_policy import (current_gameplan_policy_metadata, is_current_gameplan_recipe,
+    direction_only_rows, validate_direction_only_plan)
 
 
 PACKAGE_SCHEMA = "joint-capital-owner-package-v1"
 PLAN_SCHEMA = "joint-capital-plan-v1"
+PLAN_ONLY_MODE = "MERGED_SOURCE_INSTRUCTIONS"
 RANKING_POLICY = "existing_global_probability_order_v1"
 MAXIMUM_JSON_BYTES = 8 * 1024 * 1024
 _HASH = re.compile(r"[0-9a-f]{64}\Z")
@@ -136,7 +139,9 @@
     symbols = _symbols(package.get("frozen_symbols"))
     created = _time(package.get("created_at"), "package created_at")
     hashes = package.get("source_hashes")
-    if not isinstance(hashes, dict) or set(hashes) != _SOURCE_HASHES:
+    current_recipe = is_current_gameplan_recipe(package)
+    required_hashes = _SOURCE_HASHES - {"price_path_sha256"} if current_recipe else _SOURCE_HASHES
+    if not isinstance(hashes, dict) or set(hashes) != required_hashes:
         raise ValueError("Required native source hashes are missing")
     for name, value in hashes.items():
         _hash(value, name)
@@ -196,6 +201,22 @@
             stamp = _time(row.get(name), name)
             if stamp > created:
                 raise ValueError("Frozen forecast evidence postdates its package")
+    if current_recipe:
+        from ml.stock_direction_policy import (STOCK_DIRECTION_POLICY_ALIASES,
+            BULLISH_PROBABILITY, BEARISH_PROBABILITY)
+        if package.get("price_path") is not None:
+            raise ValueError("Current directional packages cannot require estimated price paths")
+        for item in (package, *rows):
+            if (not is_current_gameplan_recipe(item)
+                    or item.get("direction_policy_version") not in STOCK_DIRECTION_POLICY_ALIASES
+                    or item.get("direction_up_threshold") != BULLISH_PROBABILITY
+                    or item.get("direction_down_threshold") != BEARISH_PROBABILITY):
+                raise ValueError("Current package direction recipe metadata differs")
+        if package.get("content_sha256") != {"forecasts": content_sha256(rows)}:
+            raise ValueError("Owner package content hashes disagree")
+        if _hash(package.get("package_sha256"), "package") != content_sha256(_package_content(package)):
+            raise ValueError("Owner package digest disagrees with frozen contents")
+        return
     prices = package.get("price_path")
     if not isinstance(prices, dict) or not isinstance(prices.get("points"), dict):
         raise ValueError("A verified planning price path is required")
@@ -276,9 +297,10 @@
 
 def build_owner_package(*, owner_id: str, run_id: str, source_revision: str | None,
                         action_date: str, frozen_symbols: Sequence[str], created_at: str,
-                        source_hashes: Mapping, forecasts: Sequence[Mapping], price_path: Mapping,
+                        source_hashes: Mapping, forecasts: Sequence[Mapping], price_path: Mapping | None = None,
                         cross_horizon_fallback_policy: Mapping | None = None,
-                        source_reference: Mapping | None = None, late_publication_at: str | None = None) -> dict:
+                        source_reference: Mapping | None = None, late_publication_at: str | None = None,
+                        direction_only: bool = False) -> dict:
     """Seal already verified native inputs; this does not authenticate a producer."""
     package = deepcopy({"schema_version": PACKAGE_SCHEMA, "owner_id": owner_id,
         "run_id": run_id, "source_revision": source_revision, "action_date": action_date,
@@ -286,11 +308,16 @@
         "source_reference": source_reference,
         "holding_policy": SIGNAL_DRIVEN_HOLDING_POLICY, "cross_horizon_fallback_policy": cross_horizon_fallback_policy,
         "frozen_symbols": list(frozen_symbols), "created_at": created_at,
-        "source_hashes": dict(source_hashes), "forecasts": [_forecast_json(row) for row in forecasts], "price_path": dict(price_path)})
+        "source_hashes": dict(source_hashes), "forecasts": [_forecast_json(row) for row in forecasts]})
+    if direction_only:
+        package.update(current_gameplan_policy_metadata(action_date))
+    else:
+        package["price_path"] = dict(price_path or {})
     if late_publication_at is not None:
         package["late_publication_at"] = late_publication_at
-    package["content_sha256"] = {"forecasts": content_sha256(package["forecasts"]),
-                                 "price_path": content_sha256(package["price_path"])}
+    package["content_sha256"] = {"forecasts": content_sha256(package["forecasts"])}
+    if not direction_only:
+        package["content_sha256"]["price_path"] = content_sha256(package["price_path"])
     package["package_sha256"] = content_sha256(package)
     _validate_package(package)
     return package
@@ -346,7 +373,96 @@
             _number(allocation.get(name, 0), name)
 
 
-def compose_joint_plan(packages: Sequence[Mapping], snapshot: Mapping, *, action_date: str,
+def _seal_direction_plan(plan):
+    plan["plan_id"] = "joint-capital:" + content_sha256(plan)
+    plan["owner_views"] = {owner: {"plan_id": plan["plan_id"], "owner_id": owner,
+        "sole_executor_owner": plan["sole_executor_owner"],
+        "role": "EXECUTOR_AND_FORECAST_PRODUCER" if owner == plan["sole_executor_owner"] else "FORECAST_PRODUCER_ONLY",
+        "package_sha256": binding["package_sha256"], "frozen_symbols": deepcopy(binding["frozen_symbols"]),
+        "forecast_ids": [row["id"] for row in plan["forecasts"] if row["owner_id"] == owner],
+        "event_sequences": []} for owner, binding in plan["input_bindings"].items()}
+    validate_direction_only_plan(plan)
+    plan["plan_sha256"] = content_sha256(plan)
+    return plan
+
+
+def compose_joint_plan(packages: Sequence[Mapping], snapshot: Mapping | None = None, *, action_date: str,
+                       expected_universes: Mapping[str, Sequence[str]], expected_package_sha256: Mapping[str, str],
+                       account_scope_sha256: str, as_of: str, executor_owner: str,
+                       policy: StockTraderPolicy | None = None,
+                       maximum_snapshot_age_seconds: int = 900,
+                       maximum_package_age_seconds: int = 604800) -> dict:
+    """Compose current and mixed sources without estimated prices or quantities.
+
+    Two historical price-backed sources retain their existing arithmetic reader.
+    Introducing a current source uses only the original probabilities from both.
+    """
+    settings = dict(action_date=action_date, expected_universes=expected_universes,
+        expected_package_sha256=expected_package_sha256, account_scope_sha256=account_scope_sha256,
+        as_of=as_of, executor_owner=executor_owner, policy=policy,
+        maximum_snapshot_age_seconds=maximum_snapshot_age_seconds,
+        maximum_package_age_seconds=maximum_package_age_seconds)
+    if not isinstance(packages, (list, tuple)) or not any(is_current_gameplan_recipe(p) for p in packages if isinstance(p, Mapping)):
+        return _compose_legacy_joint_plan(packages, snapshot, **settings)
+    day, scope, now = _date(action_date), _hash(account_scope_sha256, "account scope"), _time(as_of, "as_of")
+    if type(maximum_package_age_seconds) is not int or not 1 <= maximum_package_age_seconds <= 604800:
+        raise ValueError("Package freshness bound must be within seven days")
+    if len(packages) != 2 or len(expected_universes) != 2:
+        raise ValueError("Both expected owner packages are required")
+    owners = set(expected_universes)
+    if executor_owner not in owners or set(expected_package_sha256) != owners:
+        raise ValueError("Both trusted owner package bindings and one executor are required")
+    selected, claimed, records = {}, set(), []
+    for package in packages:
+        _validate_package(package)
+        owner = package["owner_id"]
+        if owner not in owners or owner in selected:
+            raise ValueError("Missing, unexpected, or duplicate owner package")
+        if package["package_sha256"] != _hash(expected_package_sha256[owner], "trusted package"):
+            raise ValueError("Package differs from its independently selected source binding")
+        if package["action_date"] != day or set(package["frozen_symbols"]) != set(_symbols(expected_universes[owner])):
+            raise ValueError("Package action date or frozen universe differs from the selected source")
+        if not 0 <= (now - _time(package["created_at"], "package created_at")).total_seconds() <= maximum_package_age_seconds:
+            raise ValueError("Owner package is stale or future-dated")
+        if claimed.intersection(package["frozen_symbols"]):
+            raise ValueError("Overlapping owner symbol claims")
+        claimed.update(package["frozen_symbols"])
+        selected[owner] = package
+    if set(selected) != owners:
+        raise ValueError("Missing owner package")
+    for owner in sorted(selected):
+        package = selected[owner]
+        for row in sorted(package["forecasts"], key=lambda r: r["id"]):
+            records.append({**deepcopy(row), "id": "joint:" + content_sha256([owner, package["run_id"], row["id"]]),
+                "original_forecast_id": row["id"], "owner_id": owner, "owner_run_id": package["run_id"]})
+    # Preserve each source's historical fallback metadata; current instructions
+    # carry the prospective ownership policy and do not execute donor quotas.
+    policies = [selected[owner].get("cross_horizon_fallback_policy") for owner in sorted(selected)]
+    fallback = deepcopy(policies[0]) if policies[0] == policies[1] else None
+    if snapshot is not None and snapshot.get("account_scope_sha256") != scope:
+        raise ValueError("Observed account context belongs to a different account")
+    rows, ledger = direction_only_rows(pd.DataFrame(records), action_date=day, snapshot=snapshot)
+    ledger["cross_horizon_fallback_policy"] = deepcopy(fallback)
+    bindings = {owner: {key: deepcopy(selected[owner][key]) for key in (
+        "run_id", "source_revision", "source_revision_status", "source_reference", "frozen_symbols", "created_at",
+        "source_hashes", "content_sha256", "package_sha256")} for owner in sorted(selected)}
+    plan = {"schema_version": PLAN_SCHEMA, "status": "COMPLETE", "authority": "INFORMATIONAL_ONLY",
+        "action_date": day, "as_of": now.isoformat(), "account_scope_sha256": scope,
+        **current_gameplan_policy_metadata(day), "input_bindings": bindings, "ranking_policy": RANKING_POLICY,
+        "sole_executor_owner": executor_owner, "holding_policy": SIGNAL_DRIVEN_HOLDING_POLICY,
+        "cross_horizon_fallback_policy": deepcopy(fallback), "composition_mode": PLAN_ONLY_MODE,
+        "source_cross_horizon_fallback_policies": {owner: deepcopy(selected[owner].get("cross_horizon_fallback_policy"))
+                                                  for owner in sorted(selected)},
+        "account_snapshot_sha256": content_sha256(snapshot) if snapshot is not None else None,
+        "account_snapshot_observed_at": snapshot.get("observed_at") if snapshot is not None else None,
+        "cash_basis": "LIVE_BROKER_CAPACITY_AT_EXECUTION", "live_capital_reserved": False, "live_orders_authorized": False,
+        "forecasts": [_forecast_json(row) for row in rows.to_dict("records")], "ledger": ledger,
+        "limitations": ["BUY and SELL are directions; quantities use live cash, eligible shares and quotes.",
+            "Observed account context does not authorize orders or impose estimated price or quantity limits."]}
+    return _seal_direction_plan(plan)
+
+
+def _compose_legacy_joint_plan(packages: Sequence[Mapping], snapshot: Mapping, *, action_date: str,
                        expected_universes: Mapping[str, Sequence[str]], expected_package_sha256: Mapping[str, str],
                        account_scope_sha256: str, as_of: str, executor_owner: str,
                        policy: StockTraderPolicy | None = None,

--- a/ml/stock_direction_policy.py
+++ b/ml/stock_direction_policy.py
@@ -6,6 +6,7 @@
 from typing import Iterable, Mapping
 
 STOCK_DIRECTION_POLICY_VERSION = "stock-direction-50-01-v3"
+STOCK_DIRECTION_POLICY_ALIASES = frozenset({STOCK_DIRECTION_POLICY_VERSION, "stock-direction-5001-4999-v3"})
 BULLISH_PROBABILITY = 0.5001
 BEARISH_PROBABILITY = 0.4999
 AUTONOMOUS_HORIZON_SALES_EFFECTIVE_DATE = "2026-10-19"
@@ -34,9 +35,14 @@
     if policy_version in (None, "stock-direction-50-v2"):
         value = float(probability)
         return "BULLISH" if value > .5 else "BEARISH" if value < .5 else "NO_EDGE"
-    if policy_version != STOCK_DIRECTION_POLICY_VERSION:
+    if policy_version not in STOCK_DIRECTION_POLICY_ALIASES:
         raise ValueError("Unsupported saved stock direction policy")
     return current
+
+
+def stock_action(probability: float) -> str:
+    """Map an original probability to the inclusive current execution rule."""
+    return {"BULLISH": "BUY", "BEARISH": "SELL", "NO_EDGE": "HOLD"}[stock_direction(probability)]
 
 
 def saved_direction_policy_summary(records: Iterable[Mapping]) -> dict:
@@ -59,6 +65,8 @@
             "bearish_probability": BEARISH_PROBABILITY, "boundaries": "inclusive",
             "neutral": "strictly_between_bearish_and_bullish"},
     }
+    for version in STOCK_DIRECTION_POLICY_ALIASES:
+        known[version] = dict(known[STOCK_DIRECTION_POLICY_VERSION])
     return {"direction_policy_evaluation": "saved_frozen_direction_labels",
             "direction_policy_versions": sorted(versions),
             "unrecorded_direction_policy_forecasts": unrecorded,

--- /dev/null
+++ b/ml/gameplan_execution_policy.py
@@ -0,0 +1,133 @@
+"""Current direction-only Gameplan instructions derived from saved predictions.
+
+This pure helper preserves saved labels as provenance. It neither sizes orders
+nor reads another trader's state, providers, models or account bindings.
+"""
+from __future__ import annotations
+
+from copy import deepcopy
+from datetime import date
+import math
+from typing import Mapping
+
+from ml.stock_direction_policy import (
+    STOCK_DIRECTION_POLICY_VERSION, STOCK_DIRECTION_POLICY_ALIASES,
+    BULLISH_PROBABILITY, BEARISH_PROBABILITY, stock_action, stock_direction,
+    autonomous_horizon_sales, AUTONOMOUS_HORIZON_SALES_EFFECTIVE_DATE,
+)
+
+GAMEPLAN_RECIPE_VERSION = "direction-live-quote-v1"
+DIRECTION_ONLY_STATUS = "DIRECTION_ONLY"
+
+
+def is_current_gameplan_recipe(value: Mapping) -> bool:
+    return (value.get("gameplan_recipe_version") == GAMEPLAN_RECIPE_VERSION
+            and value.get("execution_quantity_basis") == "live_broker_capacity")
+
+
+def current_gameplan_policy_metadata(action_date) -> dict:
+    day = action_date.isoformat() if isinstance(action_date, date) else str(action_date)
+    date.fromisoformat(day)
+    return {
+        "gameplan_recipe_version": GAMEPLAN_RECIPE_VERSION,
+        "execution_quantity_basis": "live_broker_capacity",
+        "direction_policy_version": STOCK_DIRECTION_POLICY_VERSION,
+        "direction_up_threshold": BULLISH_PROBABILITY,
+        "direction_down_threshold": BEARISH_PROBABILITY,
+        "stock_execution_policy": {
+            "probability_precision": "unrounded", "buy_comparison": ">=", "sell_comparison": "<=",
+            "hold_comparison": "strictly_between_thresholds",
+            "ownership_policy_version": "account-wide-1h-4h-directional-ownership-v1",
+            "ownership_effective_action_date": AUTONOMOUS_HORIZON_SALES_EFFECTIVE_DATE,
+            "autonomous_horizons": ["15m", *(h for h in ("1h", "4h") if autonomous_horizon_sales(h, day))],
+            "unchanged_horizons": ["1d", "1w"], "reserved_sell_shares": "unavailable",
+            "price_basis": "live_bid_ask_midpoint",
+        },
+    }
+
+
+def direction_only_rows(forecasts, *, action_date, snapshot: Mapping | None = None):
+    """Derive current actions without altering historical probability or labels."""
+    import pandas as pd
+
+    metadata = current_gameplan_policy_metadata(action_date)
+    scalar = {key: metadata[key] for key in ("gameplan_recipe_version", "execution_quantity_basis",
+        "direction_policy_version", "direction_up_threshold", "direction_down_threshold")}
+
+    def observed(value):
+        if isinstance(value, bool):
+            return None
+        try:
+            result = float(value)
+        except (TypeError, ValueError):
+            return None
+        return result if math.isfinite(result) and result >= 0 else None
+
+    records = []
+    for saved in forecasts.to_dict("records"):
+        row = {key: deepcopy(value) for key, value in saved.items()
+               if not key.startswith(("direction_based_", "projected_", "trade_", "scheduled_trade_",
+                   "historical_price_", "planning_", "price_band_", "price_reference_", "fallback_"))
+               and key != "stock_execution_policy"}
+        # An immutable scientific label is evidence, not a second current gate.
+        original_direction = saved.get("source_saved_direction")
+        has_original_direction = original_direction is not None and not pd.isna(original_direction)
+        row["source_saved_direction"] = original_direction if has_original_direction else saved.get("direction")
+        source_policy = saved.get("source_saved_direction_policy_version") if has_original_direction else saved.get("direction_policy_version")
+        row["source_saved_direction_policy_version"] = None if source_policy is None or pd.isna(source_policy) else source_policy
+        probability = saved["calibrated_probability"]
+        action, direction = stock_action(probability), stock_direction(probability)
+        reason = {"BUY": "BUY_AT_CURRENT_ACCOUNT_CAPACITY", "SELL": "SELL_FROM_CURRENT_ELIGIBLE_HOLDINGS",
+                  "HOLD": "NEUTRAL"}[action]
+        if not bool(saved["execution_eligible"]):
+            action, reason = "CONTEXT", "NON_ENTRY_CONTEXT"
+        row.update(scalar, direction=direction, direction_based_action=action,
+                   direction_based_trade_quantity=0 if action == "HOLD" else None,
+                   direction_based_reason=reason)
+        if snapshot is not None:
+            held = observed(snapshot.get("held_shares", {}).get(row["symbol"]))
+            reserved = observed(snapshot.get("pending_sell_shares", {}).get(row["symbol"], 0))
+            row["observed_available_account_shares"] = max(0., held - reserved) if held is not None and reserved is not None else None
+            row["account_observed_at"] = snapshot.get("observed_at")
+            quote = snapshot.get("quotes", {}).get(row["symbol"], {})
+            for key in ("bid", "ask", "midpoint", "mark"):
+                row["observed_" + key] = observed(quote.get(key))
+        records.append(row)
+    ledger = {**metadata, "status": DIRECTION_ONLY_STATUS, "events": [], "hourly": [],
+        "ending_positions": {}, "summary": {}, "orders_placed": 0, "broker_orders_enabled": False,
+        "holding_policy": "accumulate_bullish_sell_on_bearish_no_scheduled_expiry_v1",
+        "reason": "Saved directional instructions; quantities use live cash, eligible holdings and bid/ask quotes."}
+    return pd.DataFrame(records), ledger
+
+
+def validate_direction_only_plan(plan: Mapping) -> None:
+    """Validate the current recipe without reclassifying original publications."""
+    import pandas as pd
+
+    for item in (plan, plan.get("ledger", {})):
+        if (not is_current_gameplan_recipe(item)
+                or item.get("direction_policy_version") not in STOCK_DIRECTION_POLICY_ALIASES
+                or item.get("direction_up_threshold") != BULLISH_PROBABILITY
+                or item.get("direction_down_threshold") != BEARISH_PROBABILITY):
+            raise ValueError("Current direction policy metadata differs")
+    ledger = plan["ledger"]
+    if (ledger.get("status") != DIRECTION_ONLY_STATUS or ledger.get("events") != []
+            or ledger.get("hourly") != [] or ledger.get("ending_positions") != {}
+            or ledger.get("summary") != {} or ledger.get("orders_placed") != 0
+            or ledger.get("broker_orders_enabled") is not False):
+        raise ValueError("Direction-only plan cannot carry projected fills or orders")
+    rows = plan.get("forecasts")
+    if not isinstance(rows, list) or not rows:
+        raise ValueError("Current plan requires its original forecasts")
+    expected, _ = direction_only_rows(pd.DataFrame(rows), action_date=plan["action_date"])
+    for row, derived in zip(rows, expected.to_dict("records")):
+        if (not is_current_gameplan_recipe(row)
+                or row.get("direction_policy_version") not in STOCK_DIRECTION_POLICY_ALIASES
+                or row.get("direction_up_threshold") != BULLISH_PROBABILITY
+                or row.get("direction_down_threshold") != BEARISH_PROBABILITY
+                or row.get("direction") != derived["direction"]
+                or row.get("direction_based_action") != derived["direction_based_action"]
+                or row.get("direction_based_reason") != derived["direction_based_reason"]
+                or row.get("direction_based_trade_quantity") != (0 if derived["direction_based_action"] == "HOLD" else None)
+                or any(key.startswith(("projected_", "trade_", "scheduled_trade_", "planning_")) for key in row)):
+            raise ValueError("Current directional instruction differs from its saved probability")

--- /dev/null
+++ b/tests/test_direction_only_joint_plan.py
@@ -0,0 +1,177 @@
+"""Offline source-recipe compatibility without prices, providers or live traders."""
+from copy import deepcopy
+import json
+
+import pandas as pd
+import pytest
+
+from ml.gameplan_execution_policy import (current_gameplan_policy_metadata, direction_only_rows,
+    is_current_gameplan_recipe, validate_direction_only_plan)
+from ml.independent_stock_targets import stock_target_windows, STOCK_TARGET_CONTRACT_VERSION
+from ml.joint_capital_plan import (build_owner_package, compose_joint_plan, content_sha256,
+    load_owner_package, publish_owner_package, _validate_plan_digest)
+from ml.nightly_gameplan import EXECUTION_AUTHORITY
+from ml.stock_direction_policy import (STOCK_DIRECTION_POLICY_VERSION, stock_direction,
+    stock_direction_for_policy, saved_direction_policy_summary)
+from ml.stock_target_prices import CANONICAL_STOCK_PRICE_SOURCE, stock_price_dataset
+
+DAY, NOW, SCOPE = "2026-10-20", "2026-10-20T10:00:00Z", "a" * 64
+PEER_POLICY = "stock-direction-5001-4999-v3"
+
+
+def forecasts(symbol, probability=.5001, *, version=STOCK_DIRECTION_POLICY_VERSION):
+    result = []
+    for index, spec in enumerate(stock_target_windows(pd.Timestamp(DAY).date())):
+        start = pd.Timestamp(spec["target_window_start"])
+        result.append({**spec, "id": f"forecast-{index}", "action_date": DAY, "symbol": symbol,
+            "target_window_start": start.isoformat(), "target_window_end": pd.Timestamp(spec["target_window_end"]).isoformat(),
+            "calibrated_probability": probability, "raw_probability": probability,
+            "direction": stock_direction_for_policy(probability, version),
+            "direction_policy_version": version, "direction_up_threshold": .5001, "direction_down_threshold": .4999,
+            "model_status": "RESEARCH_NOT_PROMOTED", "model_family": "offline-fixture", "decision_timestamp": NOW,
+            "information_available_at": NOW, "frozen_at": NOW, "broker_orders_enabled": False,
+            "target_contract_version": STOCK_TARGET_CONTRACT_VERSION, "execution_authority": EXECUTION_AUTHORITY,
+            "target_price_source_contract": CANONICAL_STOCK_PRICE_SOURCE,
+            "target_price_dataset": stock_price_dataset(CANONICAL_STOCK_PRICE_SOURCE),
+            "action_anchor_local": start.tz_convert("America/Los_Angeles").strftime("%H:%M") if spec["execution_eligible"] else None})
+    return result
+
+
+def price_path(symbol):
+    prior = next(pd.Timestamp(r["target_window_start"]) for r in stock_target_windows(pd.Timestamp(DAY).date()) if r["route"] == "1h@gap")
+    return {"contract_version": "conditional-hourly-planning-price-path-v1", "observed_at": NOW,
+        "price_source_contract": CANONICAL_STOCK_PRICE_SOURCE,
+        "price_dataset": stock_price_dataset(CANONICAL_STOCK_PRICE_SOURCE),
+        "lookback_sessions": 504, "minimum_samples": 2, "working_half_width_bps": 475,
+        "points": {f"{symbol}|{DAY}|{hour:02d}:00": {"status": "AVAILABLE", "symbol": symbol,
+            "action_date": DAY, "clock_local": f"{hour:02d}:00",
+            "timestamp": pd.Timestamp(f"{DAY} {hour:02d}:00", tz="America/Los_Angeles").isoformat(),
+            "planned_price_low": 10, "planned_price_mid": 10.5, "planned_price_high": 11,
+            "sample_count": 2, "reference_price": 10.5, "ratio_median": 1,
+            "reference_session": prior.tz_convert("America/Los_Angeles").date().isoformat(),
+            "reference_observed_at": prior.isoformat()} for hour in range(4, 18)}}
+
+
+def package(owner="scout", symbol="ABCL", probability=.5001, *, current=True, peer=False, **changes):
+    rows = forecasts(symbol, probability, version=PEER_POLICY if peer else STOCK_DIRECTION_POLICY_VERSION)
+    if current:
+        rows = direction_only_rows(pd.DataFrame(rows), action_date=DAY)[0].to_dict("records")
+        if peer:
+            for row in rows:
+                row["direction_policy_version"] = PEER_POLICY
+    names = ["receipt_sha256", "manifest_sha256", "forecasts_sha256"]
+    if not current:
+        names.append("price_path_sha256")
+    settings = dict(owner_id=owner, run_id=f"run-{owner}", source_revision=None,
+        action_date=DAY, frozen_symbols=[symbol], created_at=NOW,
+        source_hashes={name: content_sha256([owner, name]) for name in names}, forecasts=rows,
+        direction_only=current)
+    if not current:
+        settings["price_path"] = price_path(symbol)
+    settings.update(changes)
+    value = build_owner_package(**settings)
+    if peer:
+        value["direction_policy_version"] = PEER_POLICY
+        value["package_sha256"] = content_sha256({k: v for k, v in value.items() if k != "package_sha256"})
+    return value
+
+
+def compose(inputs=None, snapshot=None):
+    inputs = inputs or [package(peer=True), package("atlas", "AAPL")]
+    return compose_joint_plan(inputs, snapshot, action_date=DAY,
+        expected_universes={p["owner_id"]: p["frozen_symbols"] for p in inputs},
+        expected_package_sha256={p["owner_id"]: p["package_sha256"] for p in inputs},
+        account_scope_sha256=SCOPE, as_of=NOW, executor_owner="atlas")
+
+
+@pytest.mark.parametrize("horizon", ["1h", "4h", "1d", "1w"])
+@pytest.mark.parametrize("probability,action", [(.4999, "SELL"), (.49995, "HOLD"), (.5, "HOLD"),
+    (.50005, "HOLD"), (.5001, "BUY")])
+def test_current_directions_ignore_model_gates_and_missing_capacity(horizon, probability, action):
+    row = dict(id="fixture", symbol="AAPL", model_group=horizon, execution_eligible=True,
+        calibrated_probability=probability, direction="NO_EDGE", model_status="RESEARCH_NO_TARGET_HISTORY",
+        symbol_fitted_target_rows=0, symbol_route_fitted_target_rows=0)
+    original = deepcopy(row)
+    rows, ledger = direction_only_rows(pd.DataFrame([row]), action_date=DAY,
+        snapshot={"available_cash": 0, "held_shares": {}, "quotes": {}})
+    assert rows.iloc[0].direction_based_action == action
+    assert rows.iloc[0].source_saved_direction == "NO_EDGE"
+    assert row == original and ledger["events"] == []
+    quantity = rows.iloc[0].direction_based_trade_quantity
+    assert quantity == 0 if action == "HOLD" else pd.isna(quantity)
+
+
+def test_peer_policy_alias_preserves_identity_and_historical_unrecorded_status():
+    assert stock_direction_for_policy(.50005, PEER_POLICY) == "NO_EDGE"
+    assert stock_direction_for_policy(.50005, "stock-direction-50-v2") == "BULLISH"
+    assert stock_direction_for_policy(.50005, None) == "BULLISH"
+    summary = saved_direction_policy_summary([{"direction_policy_version": PEER_POLICY}, {}])
+    assert summary["direction_policy_versions"] == [PEER_POLICY]
+    assert summary["unrecorded_direction_policy_forecasts"] == 1
+    assert summary["direction_policy_cutoffs"][PEER_POLICY]["boundaries"] == "inclusive"
+
+
+def test_peer_current_package_roundtrip_preserves_exact_source_and_needs_no_prices(tmp_path):
+    value = package(peer=True)
+    before = deepcopy(value)
+    path = publish_owner_package(tmp_path, value)
+    loaded = load_owner_package(tmp_path, path, expected_sha256=value["package_sha256"])
+    assert loaded == before == value
+    assert "price_path" not in loaded and "price_path_sha256" not in loaded["source_hashes"]
+    assert loaded["direction_policy_version"] == PEER_POLICY
+    assert loaded["content_sha256"] == {"forecasts": content_sha256(loaded["forecasts"])}
+
+
+@pytest.mark.parametrize("legacy", [False, True])
+def test_current_and_mixed_composition_preserve_probabilities_without_estimated_quantities(legacy):
+    inputs = [package(peer=True, probability=.4999), package("atlas", "AAPL", .50005, current=not legacy)]
+    originals = deepcopy(inputs)
+    state = {"account_scope_sha256": SCOPE, "observed_at": NOW, "available_cash": 0,
+        "held_shares": {"ABCL": 0, "AAPL": 0}, "pending_sell_shares": {}, "quotes": {}}
+    plan = compose(inputs, state)
+    validate_direction_only_plan(plan)
+    _validate_plan_digest(plan, plan["plan_sha256"])
+    assert inputs == originals and compose(list(reversed(inputs)), state) == plan
+    assert plan["ledger"]["status"] == "DIRECTION_ONLY" and plan["ledger"]["events"] == []
+    assert plan["account_snapshot_sha256"] == content_sha256(state)
+    assert plan["source_cross_horizon_fallback_policies"] == {
+        source["owner_id"]: source["cross_horizon_fallback_policy"] for source in originals}
+    assert all(set(binding) == {"run_id", "source_revision", "source_revision_status", "source_reference",
+        "frozen_symbols", "created_at", "source_hashes", "content_sha256", "package_sha256"}
+        for binding in plan["input_bindings"].values())
+    for row in plan["forecasts"]:
+        original = next(p for p in originals if p["owner_id"] == row["owner_id"])
+        source = next(r for r in original["forecasts"] if r["id"] == row["original_forecast_id"])
+        assert row["calibrated_probability"] == source["calibrated_probability"]
+        assert row["source_saved_direction"] == source.get("source_saved_direction", source["direction"])
+        assert row["target_window_start"] == source["target_window_start"]
+        assert not any(k.startswith(("projected_", "planning_", "trade_")) for k in row)
+        if row["execution_eligible"]:
+            assert row["direction_based_action"] == ("SELL" if row["symbol"] == "ABCL" else "HOLD")
+            assert row["direction_based_trade_quantity"] == (None if row["symbol"] == "ABCL" else 0)
+
+
+@pytest.mark.parametrize("mutation", ["hash", "extra-price-hash", "cutoff", "row-recipe", "label", "content"])
+def test_current_owner_reader_rejects_malformed_or_tampered_packages(tmp_path, mutation):
+    value = package(peer=True)
+    if mutation == "hash": value["source_hashes"]["forecasts_sha256"] = "bad"
+    elif mutation == "extra-price-hash": value["source_hashes"]["price_path_sha256"] = "b" * 64
+    elif mutation == "cutoff": value["direction_up_threshold"] = .55
+    elif mutation == "row-recipe": value["forecasts"][0]["execution_quantity_basis"] = "estimated"
+    elif mutation == "label": value["forecasts"][0]["direction"] = "BEARISH"
+    else: value["content_sha256"]["forecasts"] = "b" * 64
+    path = tmp_path / "selected.json"
+    path.write_text(json.dumps(value))
+    with pytest.raises(ValueError):
+        load_owner_package(tmp_path, path, expected_sha256=value["package_sha256"])
+
+
+@pytest.mark.parametrize("mutation", ["quantity", "projected", "ledger", "policy", "action"])
+def test_current_plan_validation_rejects_legacy_execution_caps_and_wrong_directions(mutation):
+    value = compose()
+    if mutation == "quantity": value["forecasts"][0]["direction_based_trade_quantity"] = 1
+    elif mutation == "projected": value["forecasts"][0]["projected_trade_budget"] = 1
+    elif mutation == "ledger": value["ledger"]["events"] = [{"quantity": 1}]
+    elif mutation == "policy": value["direction_up_threshold"] = .55
+    else: value["forecasts"][0]["direction_based_action"] = "HOLD"
+    with pytest.raises(ValueError): validate_direction_only_plan(value)

--- a/ml/joint_capital_handoff.py
+++ b/ml/joint_capital_handoff.py
@@ -1,4 +1,4 @@
-"""Export verified saved forecasts and planning prices, without account data.
+"""Export verified saved forecast instructions, without account data.
 
 This narrow handoff validates publication and forecast contracts. It does not
 reload fitted models, training cohorts, account snapshots, or trading reports,
@@ -33,6 +33,7 @@
     "symbol_fitted_target_rows", "symbol_route_fitted_target_rows", "assumed_round_trip_cost",
     "probability_target_contract", "gameplan_variant",
     "direction_policy_version", "direction_up_threshold", "direction_down_threshold",
+    "gameplan_recipe_version", "execution_quantity_basis", "source_saved_direction", "source_saved_direction_policy_version",
 })
 _PRICE_FIELDS = frozenset({
     "contract_version", "observed_at", "price_source_contract", "price_dataset", "working_half_width_bps",
@@ -150,6 +151,42 @@
         raise ValueError("Source provenance does not bind this exact Gameplan receipt")
     return revision, {"kind": "CALLER_PINNED_SOURCE_REFERENCE", "source_revision": revision,
         "receipt_sha256": receipt_hash, "reference_sha256": provenance_hash}
+
+
+def _direction_forecasts(frame, derived, *, action_date, configuration, receipt):
+    """Retain exact saved scientific values while checking the current derivation."""
+    from ml.gameplan_execution_policy import direction_only_rows, is_current_gameplan_recipe
+    from ml.stock_direction_policy import STOCK_DIRECTION_POLICY_ALIASES
+
+    if not is_current_gameplan_recipe(configuration) or not is_current_gameplan_recipe(receipt):
+        raise ValueError("Current trade-plan recipe differs from its receipt")
+    expected, _ = direction_only_rows(frame, action_date=action_date)
+    fields = [name for name in expected if name in _FORECAST_FIELDS]
+    if (not set(fields).issubset(derived.columns) or derived.id.duplicated().any()
+            or set(derived.id) != set(expected.id)):
+        raise ValueError("Current derived directions differ from their immutable predictions")
+    expected, derived = expected[fields].copy(), derived[fields].copy()
+    # Atlas and Scout have distinct saved names for the same inclusive policy.
+    # Validate the recorded version's behavior, then retain that exact identity.
+    version = configuration.get("direction_policy_version")
+    if (not isinstance(version, str) or version not in STOCK_DIRECTION_POLICY_ALIASES
+            or version != receipt.get("direction_policy_version")
+            or not derived.direction_policy_version.eq(version).all()):
+        raise ValueError("Current trade-plan direction policy is inconsistent")
+    expected["direction_policy_version"] = version
+    for key in ("direction_up_threshold", "direction_down_threshold"):
+        if configuration.get(key) != expected[key].iloc[0] or receipt.get(key) != configuration.get(key):
+            raise ValueError("Current trade-plan direction policy is inconsistent")
+    for field in ("target_window_start", "target_window_end", "decision_timestamp", "information_available_at", "frozen_at"):
+        if field in fields:
+            derived[field] = pd.to_datetime(derived[field], utc=True, errors="raise")
+            expected[field] = pd.to_datetime(expected[field], utc=True, errors="raise")
+    try:
+        pd.testing.assert_frame_equal(derived.set_index("id").sort_index(), expected.set_index("id").sort_index(),
+                                      check_dtype=False, check_exact=True)
+    except AssertionError as exc:
+        raise ValueError("Current derived directions differ from their immutable predictions") from exc
+    return derived
 
 
 def export_owner_package(datastore_root: Path, *, gameplan_run: Path, trade_plan_run: Path,
@@ -208,7 +245,6 @@
         raise ValueError("Required forecast fields cannot be null")
     if frame.frozen_at.gt(published).any():
         raise ValueError("Frozen forecasts postdate their publication")
-    _validated_instructions(frame)
     from ml.gameplan_probability_target import probability_target_contract, probability_target_metadata
     metadata = probability_target_metadata(probability_target_contract(config))
     if (probability_target_contract(frame) != metadata["probability_target_contract"]
@@ -238,6 +274,27 @@
     fallback = config.get("cross_horizon_fallback_policy")
     if any(item.get("cross_horizon_fallback_policy") != fallback for item in (receipt, trade_config, trade_receipt)):
         raise ValueError("Frozen fallback policy differs between publications")
+    from ml.gameplan_execution_policy import is_current_gameplan_recipe
+    if is_current_gameplan_recipe(trade_config) or is_current_gameplan_recipe(trade_receipt):
+        derived_bytes = _output(trade, root, trade_manifest, "trade-plan.parquet", maximum=_PARQUET_LIMIT)
+        inputs[trade / "trade-plan.parquet"] = sha256(derived_bytes).hexdigest()
+        derived = _direction_forecasts(frame, pd.read_parquet(BytesIO(derived_bytes)), action_date=day,
+                                      configuration=trade_config, receipt=trade_receipt)
+        revision, reference = _source_reference(root, config, receipt, sha256(receipt_bytes).hexdigest(),
+            sha256(manifest_bytes).hexdigest(), revision=source_revision, provenance=source_provenance,
+            provenance_hash=source_provenance_sha256)
+        package = build_owner_package(owner_id=owner_id, run_id=game.name, source_revision=revision,
+            action_date=day, frozen_symbols=symbols, created_at=created.isoformat(),
+            source_hashes={"receipt_sha256": sha256(receipt_bytes).hexdigest(), "manifest_sha256": sha256(manifest_bytes).hexdigest(),
+                "forecasts_sha256": sha256(forecast_bytes).hexdigest()},
+            forecasts=derived.to_dict("records"), source_reference=reference,
+            cross_horizon_fallback_policy=fallback, late_publication_at=late_published, direction_only=True)
+        for path, digest in inputs.items():
+            if sha256(_read(path, root, maximum=_PARQUET_LIMIT if path.suffix == ".parquet" else _JSON_LIMIT)).hexdigest() != digest:
+                raise ValueError("Selected handoff input changed during validation")
+        _ordinary(Path(output_root), Path(output_root), directory=True)
+        return publish_owner_package(Path(output_root), package)
+    _validated_instructions(frame)
     price_bytes = _output(trade, root, trade_manifest, "planning-price-path.json", maximum=MAX_NATIVE_PRICE_BYTES)
     inputs[trade / "planning-price-path.json"] = sha256(price_bytes).hexdigest()
     prices = _object(price_bytes)

--- a/ml/joint_capital_adoption.py
+++ b/ml/joint_capital_adoption.py
@@ -47,6 +47,12 @@
     frame = pd.DataFrame(plan["forecasts"])
     from ml.stock_trader.gameplan_execution import _validated_instructions
     _validated_instructions(frame)
+    from ml.gameplan_execution_policy import is_current_gameplan_recipe, validate_direction_only_plan
+    if plan["ledger"].get("status") == "DIRECTION_ONLY":
+        if (not is_current_gameplan_recipe(plan) or plan.get("cash_basis") != "LIVE_BROKER_CAPACITY_AT_EXECUTION"
+                or any(not is_current_gameplan_recipe(row) for row in plan["forecasts"])):
+            raise ValueError("Combined current instructions differ from their recipe")
+        validate_direction_only_plan(plan)
     if (frame.empty or frame.id.duplicated().any() or set(frame.symbol) != set(symbols)
             or not frame.action_date.eq(plan["action_date"]).all()):
         raise ValueError("Combined forecast identities, symbols or dates disagree")

--- a/app/ui/gameplan_data.py
+++ b/app/ui/gameplan_data.py
@@ -95,7 +95,7 @@
     symbol: str
     horizon: str
     action: str
-    quantity: float
+    quantity: float | None
     price: float | None
     reason: str
     forecast_id: str
@@ -145,7 +145,7 @@
 
     @property
     def projection_available(self) -> bool:
-        return self.projection_status == "COMPLETE"
+        return self.projection_status in {"COMPLETE", "DIRECTION_ONLY"}
 
     @property
     def symbols(self) -> tuple[str, ...]:
@@ -304,6 +304,8 @@
 
 
 def _forecast(row: dict, *, projection_available: bool = True, unavailable_reason: str = "PRICE_REFERENCES_UNAVAILABLE") -> PlanForecast:
+    from ml.gameplan_execution_policy import is_current_gameplan_recipe
+    direction_only = is_current_gameplan_recipe(row)
     probability = _number(row["calibrated_probability"], optional=True)
     eligible = row["execution_eligible"]
     if not isinstance(eligible, bool) or (probability is not None and probability > 1):
@@ -321,6 +323,14 @@
         reason = unavailable_reason if eligible else "NON_ENTRY_CONTEXT"
     if horizon not in HORIZONS or direction not in {"BULLISH", "BEARISH", "NO_EDGE"}:
         raise GameplanError("Unsupported forecast horizon or direction")
+    if direction_only:
+        from ml.stock_direction_policy import stock_direction, BULLISH_PROBABILITY, BEARISH_PROBABILITY
+        expected = stock_direction(probability)
+        expected_action = {"BULLISH": "BUY", "BEARISH": "SELL", "NO_EDGE": "HOLD"}[expected] if eligible else "CONTEXT"
+        if (direction != expected or action != expected_action
+                or row.get("direction_up_threshold") != BULLISH_PROBABILITY
+                or row.get("direction_down_threshold") != BEARISH_PROBABILITY):
+            raise GameplanError("Current directional instruction disagrees with its probability or cutoffs")
     allowed = {"BUY", "SELL", "HOLD", "CONTEXT"} if projection_available else {"UNAVAILABLE", "CONTEXT"}
     if (action not in allowed
             or (eligible and action == "CONTEXT") or (not eligible and action != "CONTEXT")):
@@ -328,12 +338,14 @@
     start, end = _timestamp(row["target_window_start"]), _timestamp(row["target_window_end"])
     if start >= end:
         raise GameplanError("Invalid forecast target window")
-    if ((action == "BUY" and (quantity is None or quantity <= 0))
-            or (action == "SELL" and (quantity is None or quantity >= 0))
+    if ((action == "BUY" and (quantity is not None if direction_only else quantity is None or quantity <= 0))
+            or (action == "SELL" and (quantity is not None if direction_only else quantity is None or quantity >= 0))
             or (action == "HOLD" and quantity != 0)
             or (action == "CONTEXT" and quantity is not None)):
         raise GameplanError("Projected action and direction-based quantity disagree")
     price = _number(row.get("trade_price_mid"), optional=True)
+    if direction_only and price is not None:
+        raise GameplanError("Current directional instructions cannot contain an estimated execution price")
     if price is not None and price <= 0:
         raise GameplanError("Saved planning prices must be positive when available")
     donor_horizon = donor_allocation = None
@@ -358,6 +370,13 @@
 
 def _projection_metadata(ledger: dict, report: dict, frame: pd.DataFrame) -> tuple[str, str]:
     status = ledger.get("status")
+    if status == "DIRECTION_ONLY":
+        from ml.gameplan_execution_policy import is_current_gameplan_recipe
+        if (not is_current_gameplan_recipe(report) or report.get("direction_based_projection") != ledger
+                or any(not is_current_gameplan_recipe(row) for row in frame.to_dict("records"))):
+            raise GameplanError("Current directional recipe differs from its saved report")
+        _actions(ledger, tuple(_forecast(row) for row in frame.to_dict("records")), str(frame.action_date.iloc[0]))
+        return status, "Saved BUY/SELL instructions use current cash, eligible shares, orders and live quotes at execution."
     if report.get("direction_projection_status", "COMPLETE") != status:
         raise GameplanError("Saved report and direction ledger projection statuses disagree")
     if status == "COMPLETE":
@@ -476,6 +495,19 @@
 
 
 def _actions(ledger: dict, forecasts: tuple[PlanForecast, ...], session: str) -> tuple[PlannedAction, ...]:
+    if ledger.get("status") == "DIRECTION_ONLY":
+        from ml.gameplan_execution_policy import is_current_gameplan_recipe
+        if (not is_current_gameplan_recipe(ledger)
+                or any(ledger.get(key) != empty for key, empty in (
+                    ("events", []), ("hourly", []), ("ending_positions", {}), ("summary", {})))
+                or ledger.get("orders_placed") != 0 or ledger.get("broker_orders_enabled") is not False
+                or any(row.quantity is not None for row in forecasts if row.action != "HOLD")
+                or any(row.price is not None for row in forecasts)):
+            raise GameplanError("Current directional plan contains projected quantities, prices or fills")
+        return tuple(PlannedAction("direction:" + row.forecast_id, sequence, row.start,
+            row.symbol, row.horizon, row.action, None, None, row.reason, row.forecast_id,
+            source="direction_intent") for sequence, row in enumerate(
+                (item for item in forecasts if item.eligible and item.action in {"BUY", "SELL"}), 1))
     if ledger.get("version") != LEDGER_VERSION or ledger.get("status") != "COMPLETE":
         raise GameplanError("The direction ledger is not complete or supported")
     _validate_fallback_accounting(ledger, session)
@@ -687,7 +719,7 @@
     if "gameplan_variant" in frame and not frame.gameplan_variant.eq(probability_target_metadata(contract)["gameplan_variant"]).all():
         raise GameplanError("Account Gameplan variant disagrees with its frozen target")
     status, note = _projection_metadata(ledger, report, frame)
-    available = status == "COMPLETE"
+    available = status in {"COMPLETE", "DIRECTION_ONLY"}
     # Validate the entire account first, including the other producer's events.
     # Only presentation is filtered; the shared cash ledger remains untouched.
     forecasts = tuple(sorted((_forecast(row, projection_available=available) for row in frame.to_dict("records")),
@@ -763,7 +795,8 @@
                     key=lambda row: (row.start, HORIZONS.index(row.horizon), row.symbol, row.route)))
                 return Gameplan(combined_date, _timestamp(combined["as_of"]), _timestamp(binding["accepted_at"]),
                     run, run / "joint-plan.json", forecasts, _actions(combined["ledger"], forecasts, combined_date),
-                    "COMPLETE", "", "Combined plan: one account-wide budget; quantities remain subject to actual fills and cash.",
+                    combined["ledger"]["status"], "", ("Combined directions; quantities use current cash, eligible holdings and live quotes."
+                        if combined["ledger"]["status"] == "DIRECTION_ONLY" else "Combined plan: one account-wide budget; quantities remain subject to actual fills and cash."),
                     _execution_quotes(root, forecasts), combined["holding_policy"],
                     probability_target_contract(pd.DataFrame(combined["forecasts"])))
             if account is not None:
@@ -844,7 +877,7 @@
                     or read_gameplan_fallback_policy(source, selected) != fallback_policy):
                 raise GameplanError("Saved fallback policy differs from its pinned source")
         projection_status, projection_note = _projection_metadata(ledger, report, frame)
-        available = projection_status == "COMPLETE"
+        available = projection_status in {"COMPLETE", "DIRECTION_ONLY"}
         forecasts = tuple(sorted((_forecast(row, projection_available=available,
                                            unavailable_reason=SHARED_PROJECTION_UNAVAILABLE if projection_status == SHARED_PROJECTION_UNAVAILABLE else "PRICE_REFERENCES_UNAVAILABLE")
                                   for row in frame.to_dict("records")),

--- a/ml/nightly_joint_readiness.py
+++ b/ml/nightly_joint_readiness.py
@@ -106,10 +106,12 @@
             or package["owner_id"] != actor or package["action_date"] != state["action_date"]
             or package["frozen_symbols"] != state["symbols"] or package["run_id"] != source.name):
         raise ValueError("Joint readiness owner package differs from local preparation")
+    from ml.gameplan_execution_policy import is_current_gameplan_recipe
     expected_source = {"receipt_sha256": file_checksum(source / "receipt.json"),
                        "manifest_sha256": file_checksum(source / "manifest.json"),
-                       "forecasts_sha256": file_checksum(source / "forecasts.parquet"),
-                       "price_path_sha256": file_checksum(trade / "planning-price-path.json")}
+                       "forecasts_sha256": file_checksum(source / "forecasts.parquet")}
+    if not is_current_gameplan_recipe(package):
+        expected_source["price_path_sha256"] = file_checksum(trade / "planning-price-path.json")
     source_fields = ("run_id", "source_revision", "source_revision_status", "source_reference",
                      "frozen_symbols", "created_at", "source_hashes", "content_sha256", "package_sha256")
     if (package["source_hashes"] != expected_source

--- /dev/null
+++ b/tests/test_direction_only_joint_adoption.py
@@ -0,0 +1,70 @@
+"""Current packages reach both displays without inventing capacity or fills."""
+from copy import deepcopy
+
+import pandas as pd
+import pytest
+
+from app.ui.gameplan_data import load_gameplan, GameplanError, _forecast
+from ml.gameplan_execution_policy import direction_only_rows
+from ml.joint_capital_adoption import accept_joint_plan, read_accepted_joint_plan
+from ml.joint_capital_plan import build_owner_package, compose_joint_plan, content_sha256
+from test_joint_capital_plan import DAY, NOW, SCOPE, package
+
+
+def current_plan():
+    legacy = package("atlas", "AAPL")
+    peer = package("scout", "ABCL", .4)
+    rows, _ = direction_only_rows(pd.DataFrame(peer["forecasts"]), action_date=DAY)
+    current = build_owner_package(owner_id="scout", run_id=peer["run_id"], source_revision=None,
+        action_date=DAY, frozen_symbols=["ABCL"], created_at=NOW,
+        source_hashes={key: value for key, value in peer["source_hashes"].items() if key != "price_path_sha256"},
+        forecasts=rows.to_dict("records"), direction_only=True)
+    return compose_joint_plan([legacy, current], {"account_scope_sha256": SCOPE, "observed_at": NOW},
+        action_date=DAY, expected_universes={"atlas": ["AAPL"], "scout": ["ABCL"]},
+        expected_package_sha256={"atlas": legacy["package_sha256"], "scout": current["package_sha256"]},
+        account_scope_sha256=SCOPE, as_of=NOW, executor_owner="atlas")
+
+
+def adopt(root, plan, actor):
+    return accept_joint_plan(root, plan, expected_sha256=plan["plan_sha256"],
+        expected_package_sha256={key: source["package_sha256"] for key, source in plan["input_bindings"].items()},
+        expected_universes={key: source["frozen_symbols"] for key, source in plan["input_bindings"].items()},
+        account_scope_sha256=SCOPE, local_actor=actor, executor_owner="atlas", action_date=DAY, accepted_at=NOW)
+
+
+@pytest.mark.parametrize("actor", ["atlas", "scout"])
+def test_mixed_current_plan_adopts_and_displays_same_intents(tmp_path, actor):
+    plan = current_plan()
+    original = deepcopy(plan)
+    run = adopt(tmp_path, plan, actor)
+    ui = load_gameplan(tmp_path)
+    assert ui.run_directory == run
+    assert ui.projection_status == "DIRECTION_ONLY" and ui.projection_available
+    assert set(ui.symbols) == {"AAPL", "ABCL"}
+    assert {row.action for row in ui.actions} == {"BUY", "SELL"}
+    assert all(row.quantity is None and row.price is None for row in ui.actions)
+    assert read_accepted_joint_plan(tmp_path, DAY)[0] == original == plan
+    assert not (tmp_path / "controls").exists()
+
+
+@pytest.mark.parametrize("field,value", [("direction_based_trade_quantity", 1), ("trade_price_mid", 10),
+    ("direction_based_action", "HOLD"), ("direction_up_threshold", .6)])
+def test_current_forecast_rejects_estimated_capacity_and_suppressed_direction(field, value):
+    row = next(row for row in current_plan()["forecasts"] if row["direction_based_action"] == "BUY")
+    row[field] = value
+    with pytest.raises(GameplanError):
+        _forecast(row)
+
+
+def test_even_resealed_current_plan_cannot_install_fabricated_fills(tmp_path):
+    plan = current_plan()
+    plan["ledger"]["events"] = [{"action": "BUY", "quantity": 1}]
+    plan.pop("plan_sha256")
+    plan.pop("plan_id")
+    plan.pop("owner_views")
+    # A matching digest does not turn a projected fill into a current recipe.
+    plan["plan_id"] = "joint-capital:" + content_sha256(plan)
+    plan["owner_views"] = {}
+    plan["plan_sha256"] = content_sha256(plan)
+    with pytest.raises(ValueError, match="projected fills"):
+        adopt(tmp_path, plan, "atlas")

--- /dev/null
+++ b/tests/test_direction_only_joint_handoff.py
@@ -0,0 +1,222 @@
+"""Offline native export and frozen-source binding for both publication recipes."""
+import json
+from pathlib import Path
+
+import pandas as pd
+import pytest
+
+from ml import joint_capital_handoff as handoff, nightly_joint_readiness as readiness
+from ml.gameplan_execution_policy import current_gameplan_policy_metadata, direction_only_rows
+from tests.test_joint_capital_handoff import DAY, digest, export, inventory, no_network, saved, write
+
+
+def refresh(case):
+    case.gm["output_files"]["forecasts.parquet"] = inventory(case.game / "forecasts.parquet")
+    write(case.game / "manifest.json", case.gm)
+    case.gr["manifest_checksum_sha256"] = digest(case.game / "manifest.json")
+    write(case.game / "receipt.json", case.gr)
+    for value in (case.tm["configuration"], case.tr):
+        value["source_receipt_sha256"] = digest(case.game / "receipt.json")
+    case.tm["output_files"]["trade-plan.parquet"] = inventory(case.trade / "trade-plan.parquet")
+    write(case.trade / "manifest.json", case.tm)
+    case.tr["manifest_sha256"] = digest(case.trade / "manifest.json")
+    write(case.trade / "receipt.json", case.tr)
+
+
+@pytest.fixture
+def current(saved):
+    # Frozen historical labels remain scientific evidence. The new publication
+    # derives today's narrow neutral interval without changing these originals.
+    values = [.5001, .4999, .5, .50005, .49995, .6000000000000001] * 4
+    saved.frame["calibrated_probability"] = values
+    saved.frame["direction"] = ["BULLISH" if p > .5 else "BEARISH" if p < .5 else "NO_EDGE" for p in values]
+    saved.frame["direction_policy_version"] = "stock-direction-50-v2"
+    saved.frame.to_parquet(saved.game / "forecasts.parquet", index=False)
+    saved.derived, _ = direction_only_rows(saved.frame, action_date=DAY)
+    saved.derived.to_parquet(saved.trade / "trade-plan.parquet", index=False)
+    for value in (saved.tm["configuration"], saved.tr):
+        value.update(current_gameplan_policy_metadata(DAY))
+    del saved.tm["output_files"]["planning-price-path.json"]
+    (saved.trade / "planning-price-path.json").unlink()
+    refresh(saved)
+    return saved
+
+
+def test_current_native_export_preserves_originals_and_requires_no_estimated_price(current, monkeypatch):
+    original = handoff._read
+    def selected(path, *args, **kwargs):
+        assert Path(path).name not in {"planning-price-path.json", "account-snapshot.json", "model.joblib", "report.json"}
+        return original(path, *args, **kwargs)
+    monkeypatch.setattr(handoff, "_read", selected)
+    before = {path: path.read_bytes() for path in current.game.iterdir() if path.is_file()}
+    path = export(current)
+    package = json.loads(path.read_bytes())
+    assert "price_path" not in package
+    assert set(package["source_hashes"]) == {"receipt_sha256", "manifest_sha256", "forecasts_sha256"}
+    assert "private_account_balance" not in path.read_text()
+    assert package["gameplan_recipe_version"] == "direction-live-quote-v1"
+    rows = {row["id"]: row for row in package["forecasts"]}
+    for source in current.frame.to_dict("records"):
+        row = rows[source["id"]]
+        assert row["calibrated_probability"] == source["calibrated_probability"]
+        assert row["source_saved_direction"] == source["direction"]
+        assert row["source_saved_direction_policy_version"] == "stock-direction-50-v2"
+        assert row["target_price_source_contract"] == source["target_price_source_contract"]
+        assert pd.Timestamp(row["target_window_start"]) == pd.Timestamp(source["target_window_start"])
+        assert row["raw_probability"] is None
+        expected = "BULLISH" if source["calibrated_probability"] >= .5001 else "BEARISH" if source["calibrated_probability"] <= .4999 else "NO_EDGE"
+        assert row["direction"] == expected
+    assert {path: path.read_bytes() for path in before} == before
+    assert export(current) == path
+
+
+def test_current_native_export_retains_peer_recorded_policy_identity(current):
+    peer_version = "stock-direction-5001-4999-v3"
+    current.derived["direction_policy_version"] = peer_version
+    current.derived.to_parquet(current.trade / "trade-plan.parquet", index=False)
+    for value in (current.tm["configuration"], current.tr):
+        value["direction_policy_version"] = peer_version
+    refresh(current)
+    package = json.loads(export(current).read_bytes())
+    assert {row["direction_policy_version"] for row in package["forecasts"]} == {peer_version}
+
+
+def test_current_export_does_not_suppress_direction_for_saved_model_assessment(current):
+    current.frame.loc[0, ["direction", "model_status", "symbol_fitted_target_rows", "symbol_route_fitted_target_rows"]] = [
+        "NO_EDGE", "RESEARCH_NO_TARGET_HISTORY", 0, 0]
+    current.frame.to_parquet(current.game / "forecasts.parquet", index=False)
+    current.derived, _ = direction_only_rows(current.frame, action_date=DAY)
+    current.derived.to_parquet(current.trade / "trade-plan.parquet", index=False)
+    refresh(current)
+    package = json.loads(export(current).read_bytes())
+    row = next(row for row in package["forecasts"] if row["id"] == current.frame.iloc[0].id)
+    assert row["calibrated_probability"] == .5001 and row["direction"] == "BULLISH"
+    assert row["source_saved_direction"] == "NO_EDGE"
+
+
+def test_current_export_preserves_absent_historical_policy_as_unrecorded(current):
+    current.frame = current.frame.drop(columns=["direction_policy_version"])
+    current.frame.to_parquet(current.game / "forecasts.parquet", index=False)
+    current.derived, _ = direction_only_rows(current.frame, action_date=DAY)
+    current.derived.to_parquet(current.trade / "trade-plan.parquet", index=False)
+    refresh(current)
+    package = json.loads(export(current).read_bytes())
+    assert all(row["source_saved_direction_policy_version"] is None for row in package["forecasts"])
+
+
+@pytest.mark.parametrize("field,value", [
+    ("calibrated_probability", .5001000000001),
+    ("direction", "BEARISH"),
+    ("target_window_end", "2026-09-09T19:00:00Z"),
+    ("source_saved_direction", "BEARISH"),
+    ("source_saved_direction_policy_version", "unrecorded-substitute"),
+    ("gameplan_recipe_version", "other-recipe"),
+])
+def test_rebound_current_derived_rows_cannot_substitute_native_predictions(current, field, value):
+    current.derived.loc[0, field] = value
+    current.derived.to_parquet(current.trade / "trade-plan.parquet", index=False)
+    refresh(current)
+    with pytest.raises(ValueError, match="immutable predictions"):
+        export(current)
+    assert not list(current.out.iterdir())
+
+
+def test_current_export_rejects_missing_derived_public_fields(current):
+    current.derived.drop(columns=["calibrated_probability"]).to_parquet(current.trade / "trade-plan.parquet", index=False)
+    refresh(current)
+    with pytest.raises(ValueError, match="immutable predictions"):
+        export(current)
+
+
+@pytest.mark.parametrize("field,value", [
+    ("execution_quantity_basis", "projected_price"),
+    ("direction_up_threshold", .51),
+    ("direction_policy_version", "stock-direction-50-v2"),
+])
+def test_current_export_requires_consistent_saved_recipe_metadata(current, field, value):
+    current.tr[field] = value
+    write(current.trade / "receipt.json", current.tr)
+    with pytest.raises(ValueError, match="recipe|policy"):
+        export(current)
+
+
+def test_current_export_rejects_a_consistent_but_older_direction_policy_claim(current):
+    current.derived["direction_policy_version"] = "stock-direction-50-v2"
+    current.derived.to_parquet(current.trade / "trade-plan.parquet", index=False)
+    for value in (current.tm["configuration"], current.tr):
+        value["direction_policy_version"] = "stock-direction-50-v2"
+    refresh(current)
+    with pytest.raises(ValueError, match="policy"):
+        export(current)
+
+
+def test_current_export_checks_derived_artifact_manifest_bytes(current):
+    path = current.trade / "trade-plan.parquet"
+    path.write_bytes(path.read_bytes() + b" ")
+    with pytest.raises(ValueError, match="manifest"):
+        export(current)
+
+
+def test_current_export_rechecks_derived_inputs_before_publication(current, monkeypatch):
+    original = handoff.build_owner_package
+    def changed(**kwargs):
+        package = original(**kwargs)
+        path = current.trade / "trade-plan.parquet"
+        path.write_bytes(path.read_bytes() + b" ")
+        return package
+    monkeypatch.setattr(handoff, "build_owner_package", changed)
+    with pytest.raises(ValueError, match="changed during validation"):
+        export(current)
+    assert not list(current.out.iterdir())
+
+
+def contribution(case, monkeypatch):
+    package_path = export(case)
+    package = json.loads(package_path.read_bytes())
+    stats_path = case.out / "stats.json"
+    write(stats_path, {"fixture": "selected reviewed stats export"})
+    stats_run = case.root / "ml/gameplan-actuals-review-runs/native-stats"
+    stats_run.mkdir(parents=True)
+    metadata = {"producer": "Scout", "action_date": "2026-09-08", "symbols": ["ABCL"]}
+    for field, name in (("source_receipt_sha256", "receipt.json"), ("source_manifest_sha256", "manifest.json"),
+                        ("source_results_sha256", "forecast-results.parquet"), ("source_report_sha256", "report.json")):
+        (stats_run / name).write_bytes(name.encode("utf-8"))
+        metadata[field] = digest(stats_run / name)
+    frame = pd.DataFrame({"id": ["original-result"]})
+    def stats_reader(path, *, expected_sha256):
+        assert path == stats_path and expected_sha256 == digest(stats_path)
+        return metadata, frame
+    monkeypatch.setattr(readiness, "read_stats_package", stats_reader)
+    state = {"action_date": DAY, "source_session": "2026-09-08", "symbols": ["ABCL"], "steps": {
+        "local_handoff": {"output": {"package": str(package_path), "stats_package": str(stats_path),
+            "files": {str(path): digest(path) for path in (package_path, stats_path)}}},
+        "verify_display": {"output": {"source_gameplan_run": str(case.game), "plan_run": str(case.trade),
+            "stats_run": str(stats_run)}}}}
+    receipt = {"local_actor": "scout", "owner_packages": {"scout": package["package_sha256"]},
+               "stats_packages": {"Scout": digest(stats_path)}}
+    fields = ("run_id", "source_revision", "source_revision_status", "source_reference", "frozen_symbols",
+              "created_at", "source_hashes", "content_sha256", "package_sha256")
+    plan = {"input_bindings": {"scout": {key: package[key] for key in fields}}}
+    if package.get("gameplan_recipe_version") == "direction-live-quote-v1":
+        plan.update(current_gameplan_policy_metadata(DAY))
+    return state, receipt, plan, metadata, frame
+
+
+def test_current_local_contribution_binds_source_without_price_file(current, monkeypatch):
+    state, receipt, plan, metadata, frame = contribution(current, monkeypatch)
+    actual_stats, actual_frame = readiness._verify_local_contribution(current.root, state, receipt, plan)
+    assert actual_stats == metadata
+    pd.testing.assert_frame_equal(actual_frame, frame)
+    path = current.game / "forecasts.parquet"
+    path.write_bytes(path.read_bytes() + b" ")
+    with pytest.raises(ValueError, match="frozen native source"):
+        readiness._verify_local_contribution(current.root, state, receipt, plan)
+
+
+def test_legacy_local_contribution_still_binds_its_actual_price_path(saved, monkeypatch):
+    state, receipt, plan, metadata, _ = contribution(saved, monkeypatch)
+    assert readiness._verify_local_contribution(saved.root, state, receipt, plan)[0] == metadata
+    path = saved.trade / "planning-price-path.json"
+    path.write_bytes(path.read_bytes() + b" ")
+    with pytest.raises(ValueError, match="frozen native source"):
+        readiness._verify_local_contribution(saved.root, state, receipt, plan)

--- a/ml/stock_trader/gameplan_execution.py
+++ b/ml/stock_trader/gameplan_execution.py
@@ -174,6 +174,7 @@
 def _validated_instructions(frame):
     """Validate every selected slot, including rows that emit no signal."""
     from ml.stock_direction_policy import stock_direction_for_policy
+    from ml.gameplan_execution_policy import is_current_gameplan_recipe
 
     seen = set()
     instructions = []
@@ -185,7 +186,7 @@
         probability = finite(row["calibrated_probability"])
         if probability is None or not 0 <= probability <= 1:
             raise ValueError("Gameplan probability is invalid for " + str(row["id"]))
-        abstention = _saved_abstention(row)
+        abstention = False if is_current_gameplan_recipe(row) else _saved_abstention(row)
         version = row.get("direction_policy_version")
         expected = stock_direction_for_policy(probability, None if version is None or pd.isna(version) else version)
         direction = str(row["direction"]).upper()
@@ -193,6 +194,66 @@
             raise ValueError("Gameplan direction disagrees with its saved probability for " + str(row["id"]))
         instructions.append((row, probability, abstention))
     return instructions
+
+
+def accepted_direction_only_signals(plan, *, as_of, source_fingerprint):
+    """Read current due directions without inventing projected quantities.
+
+    The selected immutable plan supplies probabilities and target definitions.
+    The trader's ordinary inventory, reservation and quote controls size each
+    original forecast at execution time.
+    """
+    from ml.gameplan_execution_policy import validate_direction_only_plan
+
+    validate_direction_only_plan(plan)
+    now = utc(as_of)
+    local = now.tz_convert("America/Los_Angeles")
+    if local.date().isoformat() != plan["action_date"] or not 4 <= local.hour < 17:
+        return {}
+    frame = pd.DataFrame(plan["forecasts"])
+    required = {"id", "symbol", "model_group", "direction", "calibrated_probability",
+                "target_window_start", "target_window_end", "execution_eligible", "action_date"}
+    if required.difference(frame):
+        raise ValueError("Gameplan is missing trading instructions: " + ", ".join(sorted(required.difference(frame))))
+    if not frame.action_date.astype(str).eq(plan["action_date"]).all():
+        raise ValueError("Gameplan trading instructions differ from its action date")
+    for name in ("target_window_start", "target_window_end"):
+        frame[name] = pd.to_datetime(frame[name], utc=True, errors="raise")
+    if (frame.id.isna().any() or frame.id.duplicated().any()
+            or frame.target_window_start.isna().any() or frame.target_window_end.isna().any()
+            or (frame.target_window_end <= frame.target_window_start).any()):
+        raise ValueError("Gameplan trading IDs or holding windows are invalid")
+    frame = frame.loc[frame.execution_eligible.eq(True)].copy()
+    # Validate every executable slot before selecting the latest one. A newer
+    # HOLD or expired target must never resurrect an older bullish instruction.
+    _validated_instructions(frame)
+    due = frame.loc[frame.target_window_start.le(now)].sort_values("target_window_start")
+    due = due.drop_duplicates(["symbol", "model_group"], keep="last")
+    close = pd.Timestamp(f"{plan['action_date']} 17:00", tz="America/Los_Angeles").tz_convert("UTC")
+    signals = {}
+    for row, probability, _ in _validated_instructions(due):
+        key = (str(row["symbol"]), str(row["model_group"]))
+        start, end = utc(row["target_window_start"]), utc(row["target_window_end"])
+        later = frame.loc[frame.symbol.eq(key[0]) & frame.model_group.eq(key[1])
+                          & frame.target_window_start.gt(start), "target_window_start"]
+        deadline = min(end, close, later.min() if not later.empty else close)
+        created = utc(row.get("frozen_at", plan.get("as_of", start)))
+        if not start <= now < deadline or created > now:
+            continue
+        signals[key] = PredictionSignal(
+            symbol=key[0], primary_horizon=key[1], prediction_id=str(row["id"]),
+            decision_timestamp=utc(row.get("decision_timestamp", start)).isoformat(),
+            target_window_start=start.isoformat(), target_window_end=end.isoformat(),
+            actionable_until=deadline.isoformat(), prediction_created_at=created.isoformat(),
+            calibrated_probability=probability, assumed_round_trip_cost=0.,
+            horizon_probabilities={key[1]: probability}, model_name=str(row.get("model_family", "Gameplan")),
+            model_version=str(row.get("model_artifact", "")), source_fingerprint=source_fingerprint,
+            checkpoint_session=checkpoint_session_for_target(start),
+            target_definition_version=str(row.get("target_contract_version", "")),
+            target_price_source_contract=str(row.get("target_price_source_contract", "")),
+            planned_quantity=None,
+        )
+    return signals
 
 
 def load_execution_signals(root: Path, *, as_of):
@@ -203,6 +264,10 @@
     if selected:
         plan, _, run = selected
         _assert_execution_deployment(root, run, action_date=local.date().isoformat())
+        from ml.gameplan_execution_policy import is_current_gameplan_recipe
+        if is_current_gameplan_recipe(plan):
+            signals = accepted_direction_only_signals(plan, as_of=now, source_fingerprint=run.name)
+            return signals, (run / "accepted-plan.json", run / "joint-plan.json")
         from ml.stock_trader.catchup import catchup_signals
         return catchup_signals(plan, as_of=now, source_fingerprint=run.name), (run / "accepted-plan.json", run / "joint-plan.json")
     frame, run = execution_frame(root, action_date=local.date().isoformat())

--- /dev/null
+++ b/tests/test_direction_only_joint_execution.py
@@ -0,0 +1,167 @@
+"""Current accepted instructions execute at live capacity without projected fills."""
+from copy import deepcopy
+from dataclasses import replace
+import json
+
+import pandas as pd
+import pytest
+
+from test_independent_stock_runtime import environment, _decisions
+from test_gameplan_direction_runtime import prepare
+from ml.gameplan_execution_policy import current_gameplan_policy_metadata, direction_only_rows
+from ml.stock_trader import gameplan_execution as execution
+from ml.stock_trader import independent_runtime as runtime
+from ml.stock_trader.sizing_policy import GAMEPLAN_SIZING_POLICY
+
+
+DAY = "2026-10-20"
+
+
+def forecast(*, symbol="AAPL", horizon="1h", start="11:00", end="12:00", probability=.5001,
+             eligible=True, identity=None, **extra):
+    return dict(id=identity or f"{DAY}:{symbol}:{horizon}@{start}", symbol=symbol, model_group=horizon,
+        action_date=DAY, direction="NO_EDGE", calibrated_probability=probability, raw_probability=.4723,
+        target_window_start=f"{DAY}T{start}:00Z", target_window_end=f"{DAY}T{end}:00Z",
+        frozen_at=f"{DAY}T06:00:00Z", decision_timestamp=f"{DAY}T06:00:00Z",
+        target_contract_version="independent-stock-targets-v1",
+        target_price_source_contract="observed-multi-source-stock-v2", target_role="EXECUTION",
+        model_status="RESEARCH_NO_TARGET_HISTORY", symbol_fitted_target_rows=0,
+        symbol_route_fitted_target_rows=0, execution_eligible=eligible, **extra)
+
+
+def plan(*rows):
+    frame, ledger = direction_only_rows(pd.DataFrame(rows or [forecast()]), action_date=DAY)
+    return {**current_gameplan_policy_metadata(DAY), "action_date":DAY,
+        "as_of":f"{DAY}T06:00:00Z", "plan_sha256":"a"*64,
+        "forecasts":frame.astype(object).where(frame.notna(),None).to_dict("records"), "ledger":ledger}
+
+
+@pytest.mark.parametrize("probability,expected", [(.5001,"BUY"), (.4999,"SELL"),
+    (.50010000001,"BUY"), (.49989999999,"SELL"), (.5,"HOLD"), (.4999999,"HOLD")])
+def test_original_probability_and_target_have_no_history_or_projected_quantity_gate(probability, expected):
+    saved = plan(forecast(probability=probability))
+    before = deepcopy(saved)
+    signals = execution.accepted_direction_only_signals(saved, as_of=f"{DAY}T11:37:00Z", source_fingerprint="saved")
+    signal = signals["AAPL","1h"]
+    assert signal.calibrated_probability == probability
+    assert signal.horizon_probabilities == {"1h":probability}
+    assert signal.planned_quantity is None and signal.catchup_components == ()
+    assert signal.prediction_id == saved["forecasts"][0]["id"]
+    assert signal.target_window_start == f"{DAY}T11:00:00+00:00"
+    assert signal.target_window_end == f"{DAY}T12:00:00+00:00"
+    assert signal.target_definition_version == "independent-stock-targets-v1"
+    assert signal.target_price_source_contract == "observed-multi-source-stock-v2"
+    assert saved["forecasts"][0]["direction_based_action"] == expected
+    assert saved == before
+
+
+def test_latest_due_instruction_per_horizon_preserves_hold_and_longer_target():
+    saved = plan(forecast(start="11:00",end="15:00",probability=.7),
+                 forecast(start="12:00",end="13:00",probability=.5),
+                 forecast(start="13:00",end="14:00",probability=.4),
+                 forecast(horizon="4h",end="15:00",probability=.4999),
+                 forecast(symbol="MSFT",probability=.8,eligible=False))
+    signals = execution.accepted_direction_only_signals(saved, as_of=f"{DAY}T12:15:00Z", source_fingerprint="saved")
+    assert set(signals) == {("AAPL","1h"),("AAPL","4h")}
+    assert signals["AAPL","1h"].calibrated_probability == .5
+    assert signals["AAPL","1h"].prediction_id.endswith("@12:00")
+    assert signals["AAPL","4h"].target_window_start == f"{DAY}T11:00:00+00:00"
+    assert signals["AAPL","4h"].actionable_until == f"{DAY}T15:00:00+00:00"
+    # A newer expired slot must not restore the older overlapping target.
+    assert ("AAPL","1h") not in execution.accepted_direction_only_signals(
+        saved, as_of=f"{DAY}T14:30:00Z", source_fingerprint="saved")
+
+
+@pytest.mark.parametrize("as_of", ["2026-10-20T10:59:59Z", "2026-10-21T00:00:00Z", "2026-10-21T11:01:00Z"])
+def test_current_recipe_respects_action_date_and_existing_session_hours(as_of):
+    saved = plan(forecast(horizon="1w",end="23:59"))
+    assert execution.accepted_direction_only_signals(saved, as_of=as_of, source_fingerprint="saved") == {}
+
+
+def test_current_recipe_keeps_inclusive_peer_policy_identity_and_original_row():
+    saved = plan(forecast(probability=.4999))
+    for item in [saved, saved["ledger"], *saved["forecasts"]]:
+        item["direction_policy_version"] = "stock-direction-5001-4999-v3"
+    before = deepcopy(saved)
+    signal = execution.accepted_direction_only_signals(saved, as_of=f"{DAY}T11:01:00Z", source_fingerprint="saved")["AAPL","1h"]
+    assert signal.calibrated_probability == .4999
+    assert saved == before
+
+
+@pytest.mark.parametrize("damage", ["duplicate", "backwards", "missing_time", "different_date"])
+def test_malformed_current_execution_identity_or_window_is_rejected(damage):
+    saved = plan(forecast())
+    row = saved["forecasts"][0]
+    if damage == "duplicate":
+        saved["forecasts"].append(deepcopy(row))
+    elif damage == "backwards":
+        row["target_window_end"] = row["target_window_start"]
+    elif damage == "missing_time":
+        row["target_window_start"] = None
+    else:
+        row["action_date"] = "2026-10-19"
+    with pytest.raises(ValueError, match="trading IDs|holding windows|action date"):
+        execution.accepted_direction_only_signals(saved, as_of=f"{DAY}T11:01:00Z", source_fingerprint="saved")
+
+
+def test_accepted_current_loader_bypasses_legacy_event_projection(tmp_path, monkeypatch):
+    saved = plan(forecast())
+    run = tmp_path / "ml/joint-capital-accepted/saved"
+    monkeypatch.setattr("ml.joint_capital_adoption.read_accepted_joint_plan", lambda *_: (saved,{},run))
+    monkeypatch.setattr(execution,"_assert_execution_deployment",lambda *a,**k:None)
+    monkeypatch.setattr("ml.stock_trader.catchup.catchup_signals",lambda *a,**k:pytest.fail("Current directions have no projected events"))
+    signals,sources = execution.load_execution_signals(tmp_path,as_of=f"{DAY}T11:01:00Z")
+    assert signals["AAPL","1h"].planned_quantity is None
+    assert sources == (run/"accepted-plan.json",run/"joint-plan.json")
+
+
+def test_legacy_accepted_loader_keeps_existing_catchup_route(tmp_path, monkeypatch):
+    saved = {"action_date":DAY,"ledger":{"events":[]}}
+    run = tmp_path / "ml/joint-capital-accepted/legacy"
+    calls=[]
+    monkeypatch.setattr("ml.joint_capital_adoption.read_accepted_joint_plan", lambda *_: (saved,{},run))
+    monkeypatch.setattr(execution,"_assert_execution_deployment",lambda *a,**k:None)
+    monkeypatch.setattr("ml.stock_trader.catchup.catchup_signals",lambda p,**k:calls.append((p,k)) or {"legacy":"unchanged"})
+    signals,_ = execution.load_execution_signals(tmp_path,as_of=f"{DAY}T11:01:00Z")
+    assert signals == {"legacy":"unchanged"}
+    assert calls[0][0] is saved and calls[0][1]["source_fingerprint"] == "legacy"
+
+
+@pytest.mark.parametrize("probability,side", [(.5001,"BUY"),(.4999,"SELL")])
+def test_synthetic_runtime_refreshes_empty_ledger_plan_and_uses_live_capacity(environment,monkeypatch,probability,side):
+    env=environment
+    prepare(env,monkeypatch,probability=probability)
+    env.now=pd.Timestamp(f"{DAY}T11:01:00Z")
+    saved=plan(forecast(probability=probability))
+    run=env.root/"ml/joint-capital-accepted/saved"
+    run.mkdir(parents=True)
+    for name in ("accepted-plan.json","joint-plan.json"):
+        (run/name).write_text(json.dumps(saved))
+    monkeypatch.setattr("ml.joint_capital_adoption.read_accepted_joint_plan",lambda *_:(saved,{},run))
+    monkeypatch.setattr("ml.joint_capital_adoption.assert_accepted_execution",lambda *a,**k:True)
+    monkeypatch.setattr("ml.stock_trader.contracts.execution_symbols",lambda *a,**k:tuple(env.held))
+    original_capture=runtime.capture_portfolio_state
+    def capture(*args,symbols=None,**kwargs):
+        assert set(symbols) == set(env.held)
+        return original_capture(*args,**kwargs)
+    monkeypatch.setattr(runtime,"capture_portfolio_state",capture)
+    monkeypatch.setattr(execution,"_assert_execution_deployment",lambda *a,**k:None)
+    monkeypatch.setattr(runtime,"_assert_execution_deployment",lambda *a,**k:None)
+    monkeypatch.setattr(runtime,"_loaded_gameplan_run",lambda *a,**k:run)
+    monkeypatch.setattr(runtime,"_loaded_fallback_policy",lambda *a,**k:(None,None))
+    monkeypatch.setattr(runtime,"load_current_independent_gameplan_signals",lambda root,**k:execution.load_execution_signals(root,as_of=env.now))
+    monkeypatch.setattr("ml.stock_trader.catchup.catchup_signals",lambda *a,**k:pytest.fail("Current recipe must retain own original probability after capture"))
+    result=runtime.run_independent_stock_trader_once(env.root,execute=True,session=env.broker,
+        runtime_clock=lambda:env.now,session_managed=True,sizing_policy=GAMEPLAN_SIZING_POLICY)
+    assert result.status == "ORDERS_SUBMITTED" and result.submitted_orders == 1, result.error
+    payload,=env.broker.submissions
+    leg,=payload["orderLegCollection"]
+    assert leg["instruction"] == side and leg["quantity"] > 0
+    if side == "BUY":
+        assert leg["quantity"] > 1  # The tiny probability margin does not shrink the live cash budget.
+    recorded=_decisions(result)
+    decision,=recorded["decisions"]
+    assert decision["prediction"]["calibrated_probability"] == probability
+    assert decision["prediction"]["planned_quantity"] is None
+    assert recorded["prediction_handoff"]["direction_only_execution"]["planned_quantities"] is False
+    assert "catchup" not in recorded["prediction_handoff"]

--- a/ml/stock_trader/independent_runtime.py
+++ b/ml/stock_trader/independent_runtime.py
@@ -612,16 +612,26 @@
         if source_gameplan_run is not None and (source_gameplan_run / "accepted-plan.json").is_file():
             from ml.joint_capital_adoption import read_accepted_joint_plan
             from ml.stock_trader.catchup import catchup_signals
+            from ml.gameplan_execution_policy import is_current_gameplan_recipe
             accepted = read_accepted_joint_plan(root, timestamp.tz_convert("America/Los_Angeles").date().isoformat())
-            qualified = catchup_signals(accepted[0], as_of=timestamp, source_fingerprint=source_gameplan_run.name,
-                                       reservations=state.reservations, blocked=rejected_catchup,
-                                       abandoned=abandoned_catchup)
-            metadata["catchup"] = {"plan_sha256": accepted[0]["plan_sha256"],
-                "policy": "cumulative-due-intentions-less-fills-and-open-reservations-v1",
-                "blocked_intentions": rejected_catchup,
-                "abandoned_intentions": abandoned_catchup,
-                "net_quantities": {f"{s}/{h}": signal.planned_quantity * (1 if signal.calibrated_probability else -1)
-                                   for (s, h), signal in qualified.items()}}
+            if is_current_gameplan_recipe(accepted[0]):
+                from ml.stock_trader.gameplan_execution import accepted_direction_only_signals
+                qualified = (accepted_direction_only_signals(accepted[0], as_of=timestamp,
+                             source_fingerprint=source_gameplan_run.name) if entry_allowed else {})
+                metadata["direction_only_execution"] = {"plan_sha256": accepted[0]["plan_sha256"],
+                    "policy": "current-due-direction-live-broker-capacity-v1",
+                    "forecast_ids": [signal.prediction_id for signal in qualified.values()],
+                    "planned_quantities": False}
+            else:
+                qualified = catchup_signals(accepted[0], as_of=timestamp, source_fingerprint=source_gameplan_run.name,
+                                           reservations=state.reservations, blocked=rejected_catchup,
+                                           abandoned=abandoned_catchup)
+                metadata["catchup"] = {"plan_sha256": accepted[0]["plan_sha256"],
+                    "policy": "cumulative-due-intentions-less-fills-and-open-reservations-v1",
+                    "blocked_intentions": rejected_catchup,
+                    "abandoned_intentions": abandoned_catchup,
+                    "net_quantities": {f"{s}/{h}": signal.planned_quantity * (1 if signal.calibrated_probability else -1)
+                                       for (s, h), signal in qualified.items()}}
         elif any(Path(source).name == "receipt.json" and Path(source).resolve().parent.parent ==
                  (root / "ml/gameplan-trade-plan-runs").resolve() for source in sources):
             from ml.stock_trader.catchup import native_catchup_plan, catchup_signals

--- a/tests/test_joint_capital_adoption.py
+++ b/tests/test_joint_capital_adoption.py
@@ -229,7 +229,7 @@
         return PortfolioState(env.now.isoformat(), 100_000, 100_000, sum(env.held.values()) * 100,
             0, dict(env.held), {s: q * 100 for s, q in env.held.items()}, {}, {}, 0,
             {s: QuoteState(s, 100, 100, 100, 100, 1000, env.now.isoformat()) for s in env.held},
-            "snapshot-" + env.now.isoformat(), BROKER_ID)
+            "snapshot-" + env.now.isoformat(), BROKER_ID, broker_working_orders=())
 
     def fill(broker, ledger, **kwargs):
         evidence = []
```
