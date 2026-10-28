"""Later original outcomes reach the existing UI without changing frozen science."""
from __future__ import annotations
import json
from pathlib import Path
import socket

import pandas as pd
import pytest

from app.ui.gameplan_stats_data import load_gameplan_stats
from ml.artifacts import file_checksum, write_manifest
from ml.gameplan_actuals_review import VERSION
from ml.gameplan_evaluation import EVALUATION_COLUMNS, EVALUATION_VERSION
import ml.gameplan_stats_outcome_revision as revision

DAY="2026-10-23"
NOW="2026-10-26T19:30:00Z"
PARENT_TIME="2026-10-24T01:00:00Z"


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*a,**k): raise AssertionError("Scientific revision made a network call")
    monkeypatch.setattr(socket.socket,"connect",deny)
    monkeypatch.setattr(socket,"create_connection",deny)


def save(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,allow_nan=False)+"\n")


def forecast(symbol,route,*,status="PENDING_MATURITY",p=.4,direction="BEARISH",policy=True):
    row=dict(id=f"{DAY}:{symbol}:{route}",symbol=symbol,action_date=DAY,model_status="PROMOTED",
        model_group="4h",route=route,target_role="EXECUTION",direction=direction,actuals_status=status,
        direction_correct=None,calibrated_probability=p,model_observed_target=None,model_brier_score=None,
        actual_return=None,assumed_round_trip_cost=.001,target_window_start="2026-10-23T23:00:00Z",
        target_window_end="2026-10-26T14:00:00Z",actual_start_observed_at=None,actual_end_observed_at=None,
        probability_target_contract="raw-price-direction-v1",gameplan_variant="YG")
    if policy:row.update(direction_policy_version="stock-direction-50-01-v3",direction_up_threshold=.5001,direction_down_threshold=.4999)
    if status=="EVALUATED":
        row.update(target_window_start="2026-10-23T15:00:00Z",target_window_end="2026-10-23T19:00:00Z",
            actual_start_observed_at="2026-10-23T15:00:00Z",actual_end_observed_at="2026-10-23T19:00:00Z",
            actual_return=-.2,model_observed_target=0,model_brier_score=p*p,direction_correct=direction=="BEARISH")
    return row


def base(root,*,stamp="20261024T010000.000000Z",reviewed=PARENT_TIME,rows=None):
    rows=rows or [forecast("AAPL","4h@16:00"),forecast("DBX","4h@12:00",status="EVALUATED")]
    run=root/"ml/gameplan-actuals-review-runs"/stamp
    run.mkdir(parents=True)
    frame=pd.DataFrame(rows)
    frame["private_original_marker"]=["retained"+str(i)for i in range(len(frame))]
    frame.to_parquet(run/"forecast-results.parquet",index=False)
    producers={actor:dict(symbols=[symbol],package_sha256=letter*64,coverage_status="SAVED_FORECASTS")
        for actor,symbol,letter in [("Atlas","AAPL","a"),("Scout","DBX","b")]}
    report=dict(schema_version=VERSION,status="COMPLETE",action_date=DAY,review_mode="combined-atlas-scout",
        reviewed_at=reviewed,outcomes_through="2026-10-24T00:00:00Z",probability_target_contract="raw-price-direction-v1",
        gameplan_variant="YG",coverage_status="SAVED_FORECASTS",producers=producers,
        direction_policy_evaluation="original_saved_direction",direction_policy_versions=[],direction_policy_cutoffs={},
        unrecorded_direction_policy_forecasts=0)
    save(run/"report.json",report);(run/"Gameplan-results.md").write_text("Original immutable Stats\n")
    write_manifest(run,run_timestamp=pd.Timestamp(reviewed),input_files=[],
        output_files=["forecast-results.parquet","report.json","Gameplan-results.md"],
        configuration={k:report[k]for k in ("schema_version","action_date","probability_target_contract")})
    receipt=dict(schema_version=VERSION,status="COMPLETE",action_date=DAY,run_path=run.relative_to(root).as_posix(),
        manifest_sha256=file_checksum(run/"manifest.json"),orders_placed=0,broker_orders_enabled=False)
    save(run/"receipt.json",receipt)
    pointer=dict(schema_version=VERSION,current=dict(action_date=DAY,run_path=receipt["run_path"],receipt_sha256=file_checksum(run/"receipt.json")))
    save(root/f"ml/gameplan-actuals-review-by-date/{DAY}/run.json",pointer)
    save(root/"ml/gameplan-actuals-review-latest/run.json",pointer)
    return run


def history(root,rows):
    run=root/"ml/gameplan-evaluation-runs/20261026T192900.000000Z";run.mkdir(parents=True)
    mapped=[]
    for original in rows:
        row={name:None for name in EVALUATION_COLUMNS}
        row.update({k:v for k,v in original.items()if k in row})
        row.update(id="edition:"+original["id"],source_gameplan_run="ml/nightly-gameplan-runs/edition",
            source_forecast_id=original["id"],predicted_probability=original["calibrated_probability"],
            direction_policy_version=original.get("direction_policy_version"),
            observed_target=1,observed_return=.1,brier_score=(original["calibrated_probability"]-1)**2,
            evaluation_status="EVALUATED",evaluated_at="2026-10-26T14:10:00Z",
            observed_open_timestamp=original["target_window_start"],observed_close_timestamp=original["target_window_end"],
            target_observation_source="fmp:historical-chart:5min",target_observation_dataset="FMP.HISTORICAL_CHART_5MIN",
            target_observation_entry_input_sha256="c"*64,target_observation_exit_input_sha256="d"*64,
            direction_correct=True,raw_direction_correct=False)
        mapped.append(row)
    # Endpoint proof is an explicit scientific fixture contract. Older canonical
    # producers have not yet included these optional columns in their schema.
    columns=tuple(dict.fromkeys((*EVALUATION_COLUMNS,"direction_policy_version",
        "observed_open_timestamp","observed_close_timestamp",*revision._EVIDENCE)))
    pd.DataFrame(mapped,columns=columns).to_parquet(run/"evaluations.parquet",index=False)
    save(run/"summary.json",dict(observed_at="2026-10-26T19:29:00Z"))
    write_manifest(run,run_timestamp=pd.Timestamp(NOW),input_files=[],output_files=["evaluations.parquet","summary.json"],
        configuration=dict(first_action_date="2026-09-04"))
    save(run/"receipt.json",dict(schema_version=EVALUATION_VERSION,run_path=run.relative_to(root).as_posix(),
        manifest_checksum_sha256=file_checksum(run/"manifest.json"),orders_placed=0,broker_orders_enabled=False))
    save(root/"ml/gameplan-evaluation-latest/run.json",dict(schema_version=EVALUATION_VERSION,
        current=dict(run_path=run.relative_to(root).as_posix(),receipt_checksum_sha256=file_checksum(run/"receipt.json"))))
    return run


@pytest.fixture
def case(tmp_path):
    root=tmp_path/"datastore";run=base(root)
    source=pd.read_parquet(run/"forecast-results.parquet").to_dict("records")
    history(root,source)
    package=tmp_path/"revision.json"
    revision.export_revision(root,base_run=run,expected_base_receipt_sha256=file_checksum(run/"receipt.json"),destination=package,reviewed_at=NOW)
    return root,run,package


def adopt(case):
    root,run,package=case
    return revision.adopt_revision(root,package=package,expected_sha256=file_checksum(package),base_run=run,
        expected_base_receipt_sha256=file_checksum(run/"receipt.json"),reviewed_at=NOW)


def test_real_reader_receives_new_outcome_and_preserves_all_original_fields(case):
    root,parent,package=case
    before=pd.read_parquet(parent/"forecast-results.parquet")
    before_files={p.name:p.read_bytes()for p in parent.iterdir()if p.is_file()}
    assert load_gameplan_stats(root).metrics().evaluated==1
    run=adopt(case);review=load_gameplan_stats(root)
    assert run==review.run_directory and review.metrics().evaluated==2
    after=pd.read_parquet(run/"forecast-results.parquet")
    pd.testing.assert_frame_equal(after.iloc[[1]],before.iloc[[1]],check_exact=True,check_dtype=False)
    scientific=[key for key in before if key not in revision._OUTCOMES]
    pd.testing.assert_frame_equal(after[scientific],before[scientific],check_exact=True)
    assert all(p.read_bytes()==before_files[p.name]for p in parent.iterdir()if p.is_file())
    assert review.outcomes[0].correct is False
    assert json.loads((run/"report.json").read_bytes())["outcome_revision_parent"]["receipt.json"]==file_checksum(parent/"receipt.json")


@pytest.mark.parametrize("field,value",[
    ("calibrated_probability",.6),("direction","BULLISH"),("target_window_end","2026-10-26T15:00:00Z"),
    ("direction_policy_version",None),("direction_up_threshold",.7),("target_role","OPENING_GAP_RESEARCH"),
    ("model_status","RESEARCH_NOT_PROMOTED"),("id","foreign"),
])
def test_frozen_original_mutation_rejected_without_pointer_change(case,field,value):
    root,run,package=case;pointer=root/"ml/gameplan-actuals-review-latest/run.json";before=pointer.read_bytes()
    value_=json.loads(package.read_bytes());value_["rows"][0][field]=value;save(package,value_)
    with pytest.raises(ValueError):adopt(case)
    assert pointer.read_bytes()==before


@pytest.mark.parametrize("field,value",[
    ("actual_return",.3),("actuals_status","MATURE_AWAITING_DATA"),("direction_correct",False),
    ("model_brier_score",.99),("actual_start_observed_at",None),
])
def test_prior_evaluated_row_cannot_be_replaced(case,field,value):
    root,run,package=case;payload=json.loads(package.read_bytes());payload["rows"][1][field]=value;save(package,payload)
    with pytest.raises(ValueError):adopt(case)


@pytest.mark.parametrize("field,value",[
    ("actual_end_observed_at","2026-10-26T14:01:00Z"),
    ("actual_start_observed_at","2026-10-23T22:59:00Z"),
    ("actual_end_observed_at","2026-10-26T13:54:59Z"),
    ("evaluated_at","2026-10-26T13:59:59Z"),("evaluated_at","2026-10-26T19:31:00Z"),
    ("target_observation_entry_input_sha256","invalid"),("model_brier_score",.1),
    ("target_observation_source","C:/private/local.json"),
    ("target_observation_dataset","FMP\nprivate note"),
])
def test_bad_outcome_evidence_rejected(case,field,value):
    root,run,package=case;payload=json.loads(package.read_bytes());payload["updates"][0][field]=value
    if field in revision._OUTCOMES:payload["rows"][0][field]=value
    save(package,payload)
    with pytest.raises((ValueError,TypeError)):adopt(case)


def test_retry_finishes_pointers_after_completed_immutable_run(case,monkeypatch):
    root,parent,package=case;pointer=root/"ml/gameplan-actuals-review-latest/run.json";before=pointer.read_bytes()
    original=revision._write_json
    def fail(path,value):
        if Path(path).name=="run.json" and "by-date" in str(path):raise OSError("simulated selector failure")
        return original(path,value)
    monkeypatch.setattr(revision,"_write_json",fail)
    with pytest.raises(OSError):adopt(case)
    assert pointer.read_bytes()==before
    monkeypatch.setattr(revision,"_write_json",original)
    run=adopt(case)
    assert load_gameplan_stats(root).run_directory==run
    assert adopt(case)==run


def test_same_date_pointer_conflict_rejected_on_every_retry(case):
    root,parent,package=case;pointer=root/f"ml/gameplan-actuals-review-by-date/{DAY}/run.json"
    payload=json.loads(pointer.read_bytes());payload["current"]["receipt_sha256"]="f"*64;save(pointer,payload)
    before=pointer.read_bytes();runs=list((root/"ml/gameplan-actuals-review-runs").iterdir())
    for _ in range(2):
        with pytest.raises(ValueError,match="newer dated"):adopt(case)
    assert pointer.read_bytes()==before and list((root/"ml/gameplan-actuals-review-runs").iterdir())==runs


def test_older_revision_keeps_newer_latest_date(case):
    root,parent,package=case;latest=root/"ml/gameplan-actuals-review-latest/run.json"
    payload=json.loads(latest.read_bytes());payload["current"]["action_date"]="2026-10-26";save(latest,payload);before=latest.read_bytes()
    run=adopt(case)
    assert latest.read_bytes()==before
    assert load_gameplan_stats(root,DAY).run_directory==run


def test_portable_revision_accepts_same_science_with_distinct_local_receipt(case,tmp_path):
    origin,run,package=case;peer=tmp_path/"peer"
    local=base(peer,stamp="20261024T020000.000000Z",reviewed="2026-10-24T02:00:00Z")
    assert file_checksum(local/"receipt.json")!=file_checksum(run/"receipt.json")
    adopted=revision.adopt_revision(peer,package=package,expected_sha256=file_checksum(package),base_run=local,
        expected_base_receipt_sha256=file_checksum(local/"receipt.json"),reviewed_at=NOW)
    report=json.loads((adopted/"report.json").read_bytes())
    assert report["outcome_revision_parent"]["receipt.json"]==file_checksum(local/"receipt.json")
    assert report["outcome_revision_origin_parent"]["receipt.json"]==file_checksum(run/"receipt.json")
    assert load_gameplan_stats(peer).metrics().evaluated==2


def test_unrecorded_policy_and_original_no_edge_preserved(tmp_path):
    root=tmp_path/"root";row=forecast("AAPL","4h@16:00",p=.50005,direction="NO_EDGE",policy=False)
    run=base(root,rows=[row,forecast("DBX","4h@12:00",status="EVALUATED",policy=False)])
    history(root,[row]);package=tmp_path/"r.json"
    revision.export_revision(root,base_run=run,expected_base_receipt_sha256=file_checksum(run/"receipt.json"),destination=package,reviewed_at=NOW)
    payload=json.loads(package.read_bytes());assert "direction_policy_version"not in payload["rows"][0]
    adopt((root,run,package));out=load_gameplan_stats(root).outcomes[0]
    assert out.direction=="NO_EDGE" and out.correct is None and getattr(out,"direction_policy_version",None) is None
    saved=pd.read_parquet(load_gameplan_stats(root).run_directory/"forecast-results.parquet")
    assert "direction_policy_version"not in saved and "direction_up_threshold"not in saved


def test_no_new_outcomes_reuses_prior_publication(case):
    root,parent,package=case;run=adopt(case)
    with pytest.raises(ValueError,match="No new compatible"):
        revision.build_revision(root,base_run=run,expected_base_receipt_sha256=file_checksum(run/"receipt.json"),reviewed_at=NOW)


def test_changed_scientific_source_during_snapshot_is_rejected(case,monkeypatch):
    root,parent,package=case;original=revision.read_evaluation_history
    def mutate(*a,**k):
        value=original(*a,**k);path=value.run_directory/"summary.json";path.write_bytes(path.read_bytes()+b" ");return value
    monkeypatch.setattr(revision,"read_evaluation_history",mutate)
    with pytest.raises((ValueError,RuntimeError)):
        revision.build_revision(root,base_run=parent,expected_base_receipt_sha256=file_checksum(parent/"receipt.json"),reviewed_at=NOW)


def test_duplicate_json_and_wrong_reviewed_hash_rejected(case):
    root,parent,package=case;before=(root/"ml/gameplan-actuals-review-latest/run.json").read_bytes()
    with pytest.raises(ValueError,match="reviewed hash"):
        revision.adopt_revision(root,package=package,expected_sha256="0"*64,base_run=parent,
            expected_base_receipt_sha256=file_checksum(parent/"receipt.json"),reviewed_at=NOW)
    raw=package.read_text();package.write_text(raw.replace('{','{"schema_version":"duplicate",',1))
    with pytest.raises(ValueError,match="duplicate JSON"):adopt(case)
    assert (root/"ml/gameplan-actuals-review-latest/run.json").read_bytes()==before


def test_clock_only_maturity_truthful_across_both_owners(tmp_path):
    root=tmp_path/"root"
    a=forecast("AAPL","4h@16:00");b=forecast("DBX","4h@16:00")
    future=forecast("DBX","1w@D+5");future["model_group"]="1w";future["target_window_end"]="2026-10-30T19:30:00Z"
    run=base(root,rows=[a,b,future]);history(root,[a]);package=tmp_path/"revision.json"
    revision.export_revision(root,base_run=run,expected_base_receipt_sha256=file_checksum(run/"receipt.json"),destination=package,reviewed_at=NOW)
    payload=json.loads(package.read_bytes());assert payload["newly_mature_awaiting_ids"]==[b["id"]]
    adopted=adopt((root,run,package));frame=pd.read_parquet(adopted/"forecast-results.parquet")
    assert frame.actuals_status.tolist()==["EVALUATED","MATURE_AWAITING_DATA","PENDING_MATURITY"]
    assert frame.loc[1,"actual_return"]is None or pd.isna(frame.loc[1,"actual_return"])


def test_maturity_boundary_inclusive_and_backwards_status_rejected(tmp_path):
    root=tmp_path/"root";row=forecast("DBX","4h@16:00");row["target_window_end"]=NOW
    scored=forecast("AAPL","4h@16:00");run=base(root,rows=[scored,row]);history(root,[scored]);package=tmp_path/"r.json"
    revision.export_revision(root,base_run=run,expected_base_receipt_sha256=file_checksum(run/"receipt.json"),destination=package,reviewed_at=NOW)
    payload=json.loads(package.read_bytes());assert payload["newly_mature_awaiting_ids"]==[row["id"]]
    payload["rows"][1]["actuals_status"]="PENDING_MATURITY";save(package,payload)
    with pytest.raises(ValueError,match="changes original"):adopt((root,run,package))


def test_old_scientific_evaluation_without_endpoint_proof_is_explicit_error(case):
    root,parent,package=case
    pointer=root/"ml/gameplan-evaluation-latest/run.json"
    selected=json.loads(pointer.read_bytes())
    run=root/selected["current"]["run_path"]
    frame=pd.read_parquet(run/"evaluations.parquet")
    frame.drop(columns=["observed_open_timestamp"]).to_parquet(run/"evaluations.parquet",index=False)
    write_manifest(run,run_timestamp=pd.Timestamp(NOW),input_files=[],output_files=["evaluations.parquet","summary.json"],
        configuration=dict(first_action_date="2026-09-04"))
    receipt=json.loads((run/"receipt.json").read_bytes());receipt["manifest_checksum_sha256"]=file_checksum(run/"manifest.json");save(run/"receipt.json",receipt)
    selected["current"]["receipt_checksum_sha256"]=file_checksum(run/"receipt.json");save(pointer,selected)
    before=(root/"ml/gameplan-actuals-review-latest/run.json").read_bytes()
    with pytest.raises(ValueError,match="lacks recorded endpoint provenance"):
        revision.build_revision(root,base_run=parent,expected_base_receipt_sha256=file_checksum(parent/"receipt.json"),reviewed_at=NOW)
    assert (root/"ml/gameplan-actuals-review-latest/run.json").read_bytes()==before


@pytest.mark.parametrize("field,value",[("direction_policy_version","another-recorded-policy"),("direction_up_threshold",.7)])
def test_peer_original_policy_difference_cannot_hide_behind_older_row_whitelist(case,tmp_path,field,value):
    origin,parent,package=case;peer=tmp_path/"peer-policy"
    rows=[forecast("AAPL","4h@16:00"),forecast("DBX","4h@12:00",status="EVALUATED")]
    rows[0][field]=value
    local=base(peer,rows=rows)
    before=(peer/"ml/gameplan-actuals-review-latest/run.json").read_bytes()
    # Known current-policy cutoffs fail in the strict reader before parent comparison.
    expected = "cutoffs disagree with its policy" if field == "direction_up_threshold" else "parent publication differs"
    with pytest.raises(ValueError,match=expected):
        revision.adopt_revision(peer,package=package,expected_sha256=file_checksum(package),base_run=local,
            expected_base_receipt_sha256=file_checksum(local/"receipt.json"),reviewed_at=NOW)
    assert (peer/"ml/gameplan-actuals-review-latest/run.json").read_bytes()==before


def test_explicit_proof_fixture_does_not_depend_on_optional_producer_columns(tmp_path,monkeypatch):
    legacy=tuple(key for key in EVALUATION_COLUMNS if key not in {*revision._EVIDENCE,
        "observed_open_timestamp","observed_close_timestamp","direction_policy_version"})
    monkeypatch.setitem(globals(),"EVALUATION_COLUMNS",legacy)
    root=tmp_path/"legacy-producer";parent=base(root)
    rows=pd.read_parquet(parent/"forecast-results.parquet").to_dict("records")
    history(root,rows)
    payload=revision.build_revision(root,base_run=parent,
        expected_base_receipt_sha256=file_checksum(parent/"receipt.json"),reviewed_at=NOW)
    assert len(payload["updates"])==1
    assert payload["rows"][0]["direction_policy_version"]=="stock-direction-50-01-v3"
