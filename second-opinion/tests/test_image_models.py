"""The image ships both scorer models, and the default stays the one that was validated until the retrained one is promoted on evidence."""
import sys
from pathlib import Path

import joblib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))              # the pickled models import scoring from the service folder


def test_default_model_is_the_one_promoted_on_held_out_evidence_and_the_earlier_ones_still_ship():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "ENV MODEL_PATH=models/own_v2.joblib" in dockerfile
    for name in ("own", "own_real", "own_v2"):
        assert f"COPY models/{name}.joblib models/{name}.joblib" in dockerfile


def test_the_promoted_model_names_only_the_fault_kinds_that_held_out_testing_found_reliable():
    import json
    meta = json.loads((ROOT / "models" / "own_v2.json").read_text(encoding="utf-8"))
    model = joblib.load(ROOT / "models" / "own_v2.joblib")
    kinds = set(map(str, model.fault_model.classes_))
    assert kinds == set(meta["faults"])
    assert {"service_unreachable"} & kinds == set() and "service_unreachable" in meta["kinds_dropped"]      # 83% held out: dropped, so the scorer abstains on selector faults
    by_kind = meta["leave_one_trial_out"]["by_kind"]
    assert all(by_kind[k]["accuracy"] >= meta["kinds_kept_bar"] for k in kinds - {"gateway_fault"})


def test_both_models_load_and_name_the_same_four_causes():
    faults = {name: set(map(str, joblib.load(ROOT / "models" / name).fault_model.classes_)) for name in ("own.joblib", "own_real.joblib")}
    assert faults["own.joblib"] == faults["own_real.joblib"] == {"db_connection_pool_exhaustion", "db_disk_full", "gateway_fault", "high_memory_usage"}
