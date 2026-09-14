import json

import pytest

from datafetching.research_publication import POINTERS, snapshot_references, restore_references


def test_failed_candidate_restores_baseline_and_keeps_candidate_evidence(tmp_path):
    folder = tmp_path/'batch'; folder.mkdir()
    for name in POINTERS:
        path = tmp_path/name; path.parent.mkdir(parents=True)
        path.write_text('{"current":{"run_path":"old"}}')
    baseline = snapshot_references(tmp_path, folder, 'batch')
    candidate = {'current':{'run_path':'candidate'}}
    (tmp_path/POINTERS[0]).write_text(json.dumps(candidate))
    restore_references(tmp_path, folder, 'batch', 'candidate')
    assert all(json.loads((tmp_path/name).read_text()) == baseline[name] for name in POINTERS)
    evidence = json.loads((folder/'candidate-references.json').read_text())
    assert evidence['pointers'][POINTERS[0]] == candidate


def test_independent_publication_and_changed_baseline_cannot_be_overwritten(tmp_path):
    folder = tmp_path/'batch'; folder.mkdir()
    for name in POINTERS:
        path = tmp_path/name; path.parent.mkdir(parents=True)
        path.write_text('{"current":{"run_path":"old"}}')
    snapshot_references(tmp_path, folder, 'batch')
    (tmp_path/POINTERS[0]).write_text('{"current":{"run_path":"independent"}}')
    before = {name:(tmp_path/name).read_bytes() for name in POINTERS}
    with pytest.raises(ValueError, match='independent publication'):
        restore_references(tmp_path, folder, 'batch', 'candidate')
    assert before == {name:(tmp_path/name).read_bytes() for name in POINTERS}
    saved = json.loads((folder/'production-baseline.json').read_text())
    saved['pointers'][POINTERS[0]] = {'tampered':True}
    (folder/'production-baseline.json').write_text(json.dumps(saved))
    with pytest.raises(ValueError, match='checksum'):
        snapshot_references(tmp_path, folder, 'batch')
