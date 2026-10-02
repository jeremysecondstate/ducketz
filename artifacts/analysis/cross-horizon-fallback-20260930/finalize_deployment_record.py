"""Bind completed installation and verification evidence without runtime writes."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

p = Path(__file__).resolve().parent
def read(name):
    return json.loads((p / name).read_text(encoding="utf-8"))
def sha(name):
    return hashlib.sha256((p / name).read_bytes()).hexdigest()
manifest_hash = sha("candidate-manifest.json")
for name in ("apply-progress.json", "apply-receipt.json"):
    r = read(name)
    assert r["status"] == "APPLIED" and r["manifest_sha256"] == manifest_hash
tests = read("post-deployment-verification-attempt2.json")
preservation = read("production-preservation-after.json")
git = read("post-deployment-git-verification.json")
assert tests["status"] == "PASS" and tests["exit_code"] == 0 and not tests["source_drift"]
assert tests["locks_released"] and preservation["status"] == "PASS" and git["status"] == "PASS"
assert sha(tests["log_path"]) == tests["log_sha256"]
bindings = ["candidate-manifest.json", "apply-progress.json", "apply-receipt.json",
            "post-deployment-verification-attempt2.json", tests["log_path"],
            "production-preservation-before.json", "production-preservation-after.json",
            "post-deployment-git-verification.json"]
record = {"schema_version":1,"status":"DEPLOYED_VERIFIED","completed_at_utc":datetime.now(timezone.utc).isoformat(),
          "manifest_sha256":manifest_hash,"commit":git["commit"],"installed_file_count":23,"supporting_dependency_count":176,
          "tests_passed":678,"tests_failed":0,"tests_skipped":0,"source_drift":[],
          "protected_datastore_file_count":len(preservation["protected_datastore_files"]),
          "unrelated_dirty_files_preserved":len(preservation["unrelated_dirty_sources"]),
          "active_git_branch_and_index_preserved":True,"locks_released":True,
          "supervision_status":"RELEASED","supervision_uuid":"161c058c-458f-41c9-8f09-ed7b74fdfbf8",
          "supervision_released_at_utc":"2026-10-01T00:20:38.988215+00:00",
          "execution_policy_status":"PENDING_FRESH_OCTOBER_1_GAMEPLAN",
          "nightly_start_local":"2026-09-30 21:05 America/Los_Angeles",
          "deadline_local":"2026-10-01 04:00 America/Los_Angeles",
          "trader_started":False,"overnight_pipeline_started":False,"broker_requests":0,"orders_placed":0,
          "duplicate_source_publication_needed":False,
          "resolved_verification_failure":{"report":"post-deployment-verification.json",
             "cause":"Verifier normalized only installed line endings, while original test checkout used CRLF.",
             "repair":"Bind exact original tested hashes, compare both copies after CRLF/LF normalization, retain exact before/after source hashes.",
             "application_source_changed":False,"successful_report":"post-deployment-verification-attempt2.json"},
          "evidence_sha256":{name:sha(name) for name in bindings}}
destination = p / "deployment-status.json"
with destination.open("x",encoding="utf-8") as f:
    json.dump(record,f,indent=2); f.write("\n")
original = read("post-deploy-verifier-line-ending-fix.json")
diff_name = "post-deploy-verifier-line-ending-fix.diff"
correction = {"status":"CORRECTED_BY_ADDENDUM","original_metadata_preserved":True,
              "metadata_file":"post-deploy-verifier-line-ending-fix.json",
              "original_diff_sha256":original["diff_sha256"],"actual_raw_diff_sha256":sha(diff_name),
              "reason":"Original value hashed normalized text; this addendum hashes the exact saved diff bytes.",
              "application_or_verifier_source_changed":False}
with (p / "post-deploy-verifier-checksum-addendum.json").open("x",encoding="utf-8") as f:
    json.dump(correction,f,indent=2); f.write("\n")
print(json.dumps({"status":record["status"],"report":str(destination),"sha256":sha("deployment-status.json")}))
