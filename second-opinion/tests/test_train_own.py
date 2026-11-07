"""train_own.py on synthetic trials: loading, leave-one-trial-out, the verdict table, and the model
being servable. No cluster; the feature tables are made up with the signatures our faults have."""

import json
import sys
from pathlib import Path

import numpy as np
import pytest

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

import train_own as t   # noqa: E402
from scoring import OWN_KINDS  # noqa: E402

SERVICES = ("frontend", "api-gateway", "transaction-service", "database")
# which (service, kind) lights up for each cause
HOT = {
    "db_connection_pool_exhaustion": [("transaction-service", "pool", 40), ("api-gateway", "latency", 6)],
    "high_memory_usage": [("transaction-service", "mem", 30), ("transaction-service", "cpu", 4)],
    "db_disk_full": [("database", "disk", 50), ("transaction-service", "error", 5)],
    None: [("api-gateway", "latency", 25), ("frontend", "latency", 8)],
}


def table(label, rng):
    scores = {s: {k: float(rng.normal(0, 0.6)) for k in OWN_KINDS} for s in SERVICES}
    for service, kind, size in HOT[label]:
        scores[service][kind] = size * float(rng.uniform(0.7, 1.3))
    return scores


def write_run(tmp_path, per_label=4, seed=0):
    rng = np.random.default_rng(seed)
    lines, index = [], 0
    for label in HOT:
        for _ in range(per_label):
            index += 1
            probes = [{"when": w, "features": {"scores": table(label, rng)}} for w in ("first", "end")]
            lines.append(json.dumps({"trial": index, "label": label, "contaminated": False, "probes": probes}))
    # trials that must be ignored
    lines.append(json.dumps({"trial": 99, "label": "db_disk_full", "contaminated": True,
                             "probes": [{"features": {"scores": table("db_disk_full", rng)}}]}))
    lines.append(json.dumps({"trial": 98, "label": "db_disk_full", "probes": [{"features": {"scores": None}}]}))
    run = tmp_path / "run"
    run.mkdir()
    (run / "trials.jsonl").write_text("\n".join(lines), encoding="utf-8")
    return run


def test_load_cases_skips_contaminated_trials_and_probes_without_features(tmp_path):
    cases, skipped = t.load_cases([write_run(tmp_path)])
    assert len(cases) == 4 * 4 * 2
    assert skipped == 1
    assert {c["fault"] for c in cases} == {"db_connection_pool_exhaustion", "high_memory_usage",
                                           "db_disk_full", "gateway_fault"}
    assert all(set(c["scores"]["database"]) == set(OWN_KINDS) for c in cases)


def test_a_model_trained_on_our_signatures_names_the_service_and_the_cause(tmp_path):
    cases, _ = t.load_cases([write_run(tmp_path)])
    rows = t.leave_one_trial_out(cases)
    result = t.report(rows, 0.3)
    assert result["service_accuracy"] >= 0.95
    assert result["service_and_fault_accuracy"] >= 0.9
    true_claim, wrong_claim = result["verdicts"]["true_claim"], result["verdicts"]["wrong_claim"]
    # the property the RCAEval model failed: back true claims, reject wrong ones
    assert true_claim["agree"] / sum(true_claim.values()) >= 0.8
    assert wrong_claim["agree"] == 0


def test_a_probe_and_its_twin_are_never_split_between_train_and_test(tmp_path):
    cases, _ = t.load_cases([write_run(tmp_path, per_label=4)])
    systems = [c["system"] for c in cases]
    assert all(systems.count(s) == 2 for s in set(systems))


def test_main_refuses_to_train_on_too_little_data(tmp_path):
    run = write_run(tmp_path, per_label=1)
    with pytest.raises(SystemExit):
        t.main([str(run), "--out", str(tmp_path / "models")])


def test_main_saves_a_model_the_service_can_load(tmp_path):
    import joblib
    run = write_run(tmp_path, per_label=3)
    t.main([str(run), "--out", str(tmp_path / "models")])
    model = joblib.load(tmp_path / "models" / "own.joblib")
    assert model.kinds == OWN_KINDS
    service, fault, conf, _ = model.diagnose(table("db_disk_full", np.random.default_rng(5)))
    assert (service, fault) == ("database", "db_disk_full") and conf > 0.3
