"""The image ships both scorer models, and the default stays the one that was validated until the retrained one is promoted on evidence."""
import sys
from pathlib import Path

import joblib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))              # the pickled models import scoring from the service folder


def test_default_model_is_unchanged_and_the_retrained_one_is_opt_in():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "ENV MODEL_PATH=models/own.joblib" in dockerfile
    assert "COPY models/own.joblib models/own.joblib" in dockerfile and "COPY models/own_real.joblib models/own_real.joblib" in dockerfile


def test_both_models_load_and_name_the_same_four_causes():
    faults = {name: set(map(str, joblib.load(ROOT / "models" / name).fault_model.classes_)) for name in ("own.joblib", "own_real.joblib")}
    assert faults["own.joblib"] == faults["own_real.joblib"] == {"db_connection_pool_exhaustion", "db_disk_full", "gateway_fault", "high_memory_usage"}
