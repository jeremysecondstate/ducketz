# YG execution-source guard validation

Recorded 2026-09-18 07:28:36 UTC.

The optional per-date deployment registry now governs entry-source consumption in the manual Gameplan and legacy fixed readers. PREPARING or an unselected OG source rejects new instructions. With no registered deployment the existing reader contract remains. Fixed and learned preflights run this check after the worker wakes; the sleeping session path is unchanged.

The runtime retains the exact loaded publication identity: manifest-bound source receipt paths for verified/fixed/recovery readers, and the existing saved run-name fingerprint for the manual instruction reader. It records `source_gameplan_run` in decision handoff evidence. The guard checks that loaded source before slot work, after broker capture, before durable reservation and immediately before HTTP POST. A later current-pointer change cannot turn an already loaded OG decision into YG.

Existing owned scheduled EXIT decisions remain independent of entry deployment. Directional bearish sales are instructions and require the selected source. A deployment failure clears pending entry signals while existing exit management continues, then reports `INDEPENDENT_TARGET_PLAN_UNAVAILABLE` or `OWNED_EXITS_SUBMITTED_WITH_ENTRY_PLAN_UNAVAILABLE` with its error. Per-forecast ownership, reservations, slot claims, controls, risk limits, current quotes and broker identity checks are unchanged.

## Verification

Final command, repository `.venv` Python:

```powershell
python -B -m pytest -q tests/test_gameplan_deployment_execution.py tests/test_independent_stock_session.py tests/test_gameplan_quote_recovery.py tests/test_independent_stock_signals.py tests/test_independent_stock_runtime.py tests/test_gameplan_direction_runtime.py tests/test_gameplan_execution.py tests/test_independent_stock_enrichment.py::test_new_feature_contract_and_runtime_transport
```

Result: **171 passed**, 40 existing joblib/NumPy deprecation warnings, 93.09 seconds, exit 0. Diff whitespace check passed. Tests use temporary files and synthetic brokers only.

The fifteen new deployment cases cover unregistered legacy behavior, native PREPARING/ACTIVE state, exact YG identity, unresolved identity, immutable receipt/report tampering, manual/fixed loading and preflight, quote-recovery fallback, pre-broker/pre-slot rejection, stale loaded OG during capture and at POST, and existing owned exits.

Two stale assertions were corrected without changing production trading behavior:

- The original consecutive-bullish test still expected expiry sells. The exact git HEAD test was rerun with the deployment guard patched to a no-op and reproduced the same BUY-versus-SELL failure. Evidence: `stale-expiry-test-repro.txt` beside this file. The corrected test verifies three bullish accumulations followed by one bearish sale, with each forecast reserved once.
- The enrichment transport test hard-coded 21 features from the seven-symbol universe. Unchanged HEAD training/model/market-feature modules declare 25 features for the eleven-symbol universe. The test now compares the entire declared feature tuple, including order, rather than a stale count. No enrichment production module changed.

The deployment module was independently reviewed. Entry guard uses the exact supplied path and pinned receipt checksum, never the latest pointer as a substitute. Activation's initial implementation required all tail stages within one terminal report; the root owner was advised to verify resume ancestry if a native tail resume becomes necessary, plus native receipt/log sizes alongside hashes. Native prepare/activate integration and production activation remain root-owned.

This subtask did not acquire supervision, call live providers/brokers, start a trader, enable controls/schedules, place orders, or activate a production publication.
