"""eval_real.py on synthetic trials: the comparison runs, holds each real trial out, and the saved model is servable. No cluster."""

import sys
from pathlib import Path

import joblib

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "tests"))

import eval_real as ev          # noqa: E402
import train_own as t           # noqa: E402
from test_train_own import write_run  # noqa: E402


def test_comparison_holds_real_trials_out_and_saves_a_servable_model(tmp_path):
    old = write_run(tmp_path / "old", per_label=3, seed=0) if (tmp_path / "old").mkdir() is None else None
    real = write_run(tmp_path / "real", per_label=1, seed=1) if (tmp_path / "real").mkdir() is None else None
    models = tmp_path / "models"
    t.main([str(old), "--out", str(models)])                     # stands in for the shipped model
    out = ev.main(["--old", str(old), "--real", str(real), "--current", str(models / "own.joblib"), "--save", "own_real", "--out", str(models)])

    assert out["real_trials"] == 4 and out["real_cases"] == 8
    for key in ("current_model", "retrained_model_leave_one_real_trial_out"):
        m = out[key]
        assert m["cases"] == 8 and 0 <= m["service_ok"] <= 8
        assert set(m["wrong_claim"]) == {"agree", "disagree", "no_opinion"} and sum(m["wrong_claim"].values()) == 8
        assert set(m["by_cause"]) == {"db_connection_pool_exhaustion", "high_memory_usage", "db_disk_full", None}
    # easy synthetic faults: a model that never saw the held-out trial still finds the right service
    assert out["retrained_model_leave_one_real_trial_out"]["service_ok"] >= 7
    saved = joblib.load(models / "own_real.joblib")
    assert set(map(str, saved.fault_model.classes_)) == {"db_connection_pool_exhaustion", "high_memory_usage", "db_disk_full", "gateway_fault"}
    assert (models / "own_real.json").exists()


def test_it_refuses_to_compare_on_too_few_real_trials(tmp_path):
    import pytest
    (tmp_path / "o").mkdir()
    (tmp_path / "r").mkdir()
    old = write_run(tmp_path / "o", per_label=4, seed=0)
    real = write_run(tmp_path / "r", per_label=0, seed=1)
    with pytest.raises(SystemExit):
        ev.main(["--old", str(old), "--real", str(real), "--current", str(old)])
