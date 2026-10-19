# October 19 account planner compatibility correction

Actor: Atlas. Task: the October 19 global direction-policy correction. Scope: shared compatibility behavior for Atlas and Scout installations that already contain the legacy account planner.

The reviewed main base, `6792abd38873b3aab98db82b7f091525a49a428b`, does not contain `ml/account_gameplan/planner.py` or its legacy account package. This document carries the exact task-only change for existing installations. Apply it by field and hunk to the installed legacy implementation while preserving other owners' changes. The common-main change does not recreate the removed package.

Each frozen producer is validated against its own recorded direction version and thresholds. Valid v2 and v3 sources can therefore be combined immediately. The existing cash ledger interprets their probabilities using the current policy. Original source labels, probabilities, package identities and source files remain unchanged; the resulting report identifies the current policy and records each source's original version. Unsupported versions, altered thresholds, inconsistent row metadata, invalid labels, and existing non-directional source conflicts still fail validation.

The code review removed only the equality requirement between historical direction policies. The existing holding policy, forecast coverage, immutable source reopening, price-source compatibility, symbol membership, account budget and canonical projection checks remain in place. No peer acceptance, source transition wait, broker request, model run or trader control was added.

Atlas local source baseline: `90704d21801f60426493bd6cc650a7fac38d4f61`. The initial and final SHA-256 values below bind whole local files; the unified diff binds only this task's text change. Diff lines use LF.

| File | Initial SHA-256 | Final SHA-256 |
| --- | --- | --- |
| `ml/account_gameplan/planner.py` | `76429f03524371f0131e81475183b3de29db6d47e08722eced4302bd04f0b6f8` | `e34ae23b3478e0eea8805e4028d845e0b30d6d3340c187b10230b94bfb5f441e` |
| `tests/test_account_gameplan_planner.py` | `6002492bbb592a1b6b72c38dc38049c73534b3b990c3ad1ea4685302817cf84c` | `e20c9bd4ca43a349ba3d6c7102816a440c31b2a329e031b49c08777b9d91a273` |

Offline checks on the final source:

```text
.venv/Scripts/python.exe -B -m pytest tests/test_gameplan_policy_revision.py tests/test_joint_capital_handoff.py tests/test_account_gameplan_planner.py -q -p no:cacheprovider
72 passed in 15.58s

.venv/Scripts/python.exe -B -m pytest tests/test_account_gameplan_sources.py tests/test_account_gameplan_joint_interop.py -q -p no:cacheprovider
47 passed in 12.22s
```

The added planner fixtures cover two frozen v2 sources, mixed v2/v3 sources, an old narrow-edge forecast interpreted by the current policy, invalid recorded versions, and preserved source bytes. The existing account budget, publication, byte-verification and canonical joint-composition tests also passed. These fixture results do not claim peer installation or a running-process update.

```diff
--- a/ml/account_gameplan/planner.py
+++ b/ml/account_gameplan/planner.py
@@ -21,8 +21,8 @@
 
 VERSION = "combined-account-gameplan-v1"
 _POLICIES = ("probability_target_contract", "target_price_source_contract", "target_price_dataset",
-             "cross_horizon_fallback_policy", "holding_policy", "direction_policy_version",
-             "direction_up_threshold", "direction_down_threshold")
+             "cross_horizon_fallback_policy", "holding_policy")
+_DIRECTION_POLICIES = ("direction_policy_version", "direction_up_threshold", "direction_down_threshold")
 
 
 def encoded(value) -> bytes:
@@ -52,13 +52,10 @@
     if len(sources) != 2 or len({s.metadata["producer_id"] for s in sources}) != 2:
         raise ValueError("Exactly two distinct pinned producers are required")
     first = sources[0].metadata
-    from ml.stock_direction_policy import (STOCK_DIRECTION_POLICY_VERSION,
-                                           BULLISH_PROBABILITY, BEARISH_PROBABILITY)
-    if (first.get("holding_policy") != "accumulate_bullish_sell_on_bearish_no_scheduled_expiry_v1"
-            or first.get("direction_policy_version") != STOCK_DIRECTION_POLICY_VERSION
-            or first.get("direction_up_threshold") != BULLISH_PROBABILITY
-            or first.get("direction_down_threshold") != BEARISH_PROBABILITY):
-        raise ValueError("Combined planning requires the selected current manual holding and direction policies")
+    from ml.stock_direction_policy import (stock_direction_for_policy, BULLISH_PROBABILITY, BEARISH_PROBABILITY)
+    from ml.stock_trader.gameplan_execution import _validated_instructions
+    if first.get("holding_policy") != "accumulate_bullish_sell_on_bearish_no_scheduled_expiry_v1":
+        raise ValueError("Combined planning requires the selected current manual holding policy")
     frames, points, membership, evidence = [], {}, {}, []
     for source in sorted(sources, key=lambda s: s.metadata["producer_id"]):
         meta = source.metadata
@@ -66,6 +63,12 @@
             raise ValueError("Producer action sessions differ")
         if any(meta.get(key) != first.get(key) for key in _POLICIES):
             raise ValueError("Producer source or operating policies differ")
+        version = meta.get("direction_policy_version")
+        stock_direction_for_policy(.5, version)
+        up, down = ((.5, .5) if version in (None, "stock-direction-50-v2")
+                    else (BULLISH_PROBABILITY, BEARISH_PROBABILITY))
+        if meta.get("direction_up_threshold") != up or meta.get("direction_down_threshold") != down:
+            raise ValueError("Combined planning requires the selected current manual holding policy and valid recorded direction policies")
         symbols = tuple(meta["symbols"])
         if not symbols or len(set(symbols)) != len(symbols):
             raise ValueError("Invalid producer symbol registry")
@@ -78,6 +81,11 @@
                 or frame.id.isna().any() or frame.id.duplicated().any()
                 or not frame.action_date.astype(str).eq(meta["action_date"]).all()):
             raise ValueError("Incomplete or conflicting producer forecast coverage")
+        if any(key not in frame or not frame[key].eq(meta.get(key)).all() for key in _DIRECTION_POLICIES):
+            raise ValueError("Producer forecast directions differ from their recorded policy metadata")
+        # Frozen labels retain their original policy. The cash ledger applies
+        # current directions after validating those original source labels.
+        _validated_instructions(frame)
         frame["original_forecast_id"] = frame.id.astype(str)
         frame["producer_id"] = meta["producer_id"]
         frame["source_receipt_sha256"] = meta["source_receipt_sha256"]
@@ -94,6 +102,7 @@
                          "bundle_manifest_sha256": source.manifest_sha256,
                          "source_gameplan_run": meta["source_gameplan_run"],
                          "deployment_status": meta.get("deployment_status", "NO_REGISTERED_HANDOFF"),
+                         "source_direction_policy_version": version,
                          "source_published_at": meta["source_published_at"]})
     forecasts = pd.concat(frames, ignore_index=True)
     if forecasts.id.duplicated().any():
@@ -141,6 +150,7 @@
 
 def project_account_plan(sources, snapshot, *, observed_at, policy=None):
     """Read-only projection of pinned inputs through Scout's one account ledger."""
+    from ml.stock_direction_policy import STOCK_DIRECTION_POLICY_VERSION, BULLISH_PROBABILITY, BEARISH_PROBABILITY
     policy = policy or StockTraderPolicy()
     policy.validate()
     now = aware(observed_at)
@@ -206,6 +216,9 @@
               "joint_composition": joint,
               "joint_package_pins": {p["owner_id"]: p["package_sha256"] for p in packages},
               **{key: sources[0].metadata.get(key) for key in _POLICIES},
+              "direction_policy_version": STOCK_DIRECTION_POLICY_VERSION,
+              "direction_up_threshold": BULLISH_PROBABILITY,
+              "direction_down_threshold": BEARISH_PROBABILITY,
               "cross_horizon_fallback_policy": fallback, "direction_based_projection": ledger,
               "orders_placed": 0, "broker_orders_enabled": False,
               "execution_authority": "INFORMATIONAL_ACCOUNT_PLAN",
--- a/tests/test_account_gameplan_planner.py
+++ b/tests/test_account_gameplan_planner.py
@@ -86,6 +86,58 @@
     assert "Ending shares:" in text
 
 
+def test_frozen_v2_sources_project_current_policy_without_rewriting_inputs(inputs):
+    from ml.stock_direction_policy import STOCK_DIRECTION_POLICY_VERSION
+
+    sources, state = inputs
+    originals = [source.forecasts.copy(deep=True) for source in sources]
+    rows, report, _ = project_account_plan(sources, state, observed_at=state["observed_at"])
+    assert rows.direction_policy_version.eq("stock-direction-50-v2").all()
+    assert report["direction_policy_version"] == STOCK_DIRECTION_POLICY_VERSION
+    for source, original in zip(sources, originals):
+        pd.testing.assert_frame_equal(source.forecasts, original)
+        assert source.metadata["direction_policy_version"] == "stock-direction-50-v2"
+
+
+def test_mixed_v2_and_v3_directions_project_one_current_policy(inputs):
+    from ml.stock_direction_policy import STOCK_DIRECTION_POLICY_VERSION, BULLISH_PROBABILITY, BEARISH_PROBABILITY
+
+    sources, state = inputs
+    # Freeze an independently produced v3 package; the v2 source is unchanged.
+    frame = sources[1].forecasts.copy(deep=True)
+    frame["calibrated_probability"] = .50005
+    frame["direction"] = "NO_EDGE"
+    frame["direction_policy_version"] = STOCK_DIRECTION_POLICY_VERSION
+    frame["direction_up_threshold"] = BULLISH_PROBABILITY
+    frame["direction_down_threshold"] = BEARISH_PROBABILITY
+    sources[1] = reseal_fixture(sources[1], forecasts=frame)
+    rows, report, _ = project_account_plan(sources, state, observed_at=state["observed_at"])
+    current = rows.loc[rows.producer_id.eq(sources[1].metadata["producer_id"])]
+    assert current.direction.eq("NO_EDGE").all()
+    assert current.loc[current.execution_eligible].direction_based_action.eq("HOLD").all()
+    assert set(rows.direction_policy_version) == {"stock-direction-50-v2", STOCK_DIRECTION_POLICY_VERSION}
+    assert report["direction_policy_version"] == STOCK_DIRECTION_POLICY_VERSION
+    assert {source["source_direction_policy_version"] for source in report["sources"]} == {"stock-direction-50-v2", STOCK_DIRECTION_POLICY_VERSION}
+
+
+def test_old_narrow_edge_is_validated_then_interpreted_by_current_policy(inputs):
+    sources, state = inputs
+    sources[0].forecasts["calibrated_probability"] = .50005
+    # The original v2 Bullish label remains valid evidence for that source.
+    sources[0] = reseal_fixture(sources[0])
+    rows, _, _ = project_account_plan(sources, state, observed_at=state["observed_at"])
+    old = rows.loc[rows.producer_id.eq(sources[0].metadata["producer_id"])]
+    assert old.direction.eq("BULLISH").all()
+    assert old.loc[old.execution_eligible].direction_based_action.eq("HOLD").all()
+
+
+def test_unknown_or_inconsistent_recorded_direction_policy_is_rejected(inputs):
+    sources, _ = inputs
+    sources[0].metadata["direction_policy_version"] = "unknown"
+    with pytest.raises(ValueError, match="Unsupported saved stock direction policy"):
+        combine_inputs(sources)
+
+
 def test_eleven_symbols_each_share_one_account_budget(inputs):
     templates, _ = inputs
     expanded, symbols = [], []
```
