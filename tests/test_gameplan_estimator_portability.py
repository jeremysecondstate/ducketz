"""A CLI artifact must be usable by a different interpreter's joblib.load."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import numpy as np
import pandas as pd
import pytest
from sklearn.dummy import DummyClassifier

from ml.gameplan_estimators import ProbabilityBlend


def test_probability_blend_preserves_existing_convex_scores():
    frame = pd.DataFrame({"value": [0, 1, 2, 3]})
    tree = DummyClassifier(strategy="prior").fit(frame, [0, 0, 0, 1])
    neural = DummyClassifier(strategy="prior").fit(frame, [0, 1, 1, 1])
    blend = ProbabilityBlend(tree, neural, neural_weight=0.75)
    expected = 0.25 * tree.predict_proba(frame) + 0.75 * neural.predict_proba(frame)
    np.testing.assert_array_equal(blend.predict_proba(frame), expected)


@pytest.mark.parametrize("weight", [0.25, 0.5, 0.75])
def test_cli_alias_pickle_loads_in_fresh_process_without_main_alias(tmp_path, weight):
    repository = Path(__file__).resolve().parents[1]
    model = tmp_path / "model.joblib"
    producer = """
import sys
import joblib
import pandas as pd
from sklearn.dummy import DummyClassifier
from ml.nightly_gameplan import _ProbabilityBlend
frame = pd.DataFrame({'value': [0, 1, 2, 3]})
tree = DummyClassifier(strategy='prior').fit(frame, [0, 0, 0, 1])
neural = DummyClassifier(strategy='prior').fit(frame, [0, 1, 1, 1])
joblib.dump({'estimator': _ProbabilityBlend(tree, neural, neural_weight=float(sys.argv[2]))}, sys.argv[1])
"""
    consumer = """
import sys
import json
import joblib
import pandas as pd
bundle = joblib.load(sys.argv[1])
estimator = bundle['estimator']
print(json.dumps({'module': type(estimator).__module__, 'scores': estimator.predict_proba(pd.DataFrame({'value': [9]})).tolist()}))
"""
    subprocess.run([sys.executable, "-c", producer, str(model), str(weight)],
                   cwd=repository, check=True, capture_output=True, text=True)
    loaded = subprocess.run([sys.executable, "-c", consumer, str(model)],
                            cwd=repository, check=True, capture_output=True, text=True)
    result = json.loads(loaded.stdout)
    assert result["module"] == "ml.gameplan_estimators"
    expected_positive = (1 - weight) * 0.25 + weight * 0.75
    np.testing.assert_allclose(result["scores"], [[1 - expected_positive, expected_positive]])
