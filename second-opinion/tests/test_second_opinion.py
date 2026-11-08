"""Tests for the second-opinion service: scoring, the verdict, and the HTTP endpoint.

Prometheus is faked with synthetic metrics, and the scorer is a small model trained on
synthetic cases, so none of this needs the RCAEval data or a cluster.
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

import app as svc          # noqa: E402
import opinion as op       # noqa: E402
import prometheus as prom  # noqa: E402
import scoring             # noqa: E402

CHAIN = ("frontend", "api-gateway", "transaction-service", "database")


def metrics(faulty_service=None, kind="mem", factor=6.0, n=200, inject=100, seed=0):
    rng = np.random.default_rng(seed)
    cols = {"time": np.arange(n) + 1_000}
    for s in CHAIN:
        for k in scoring.KINDS:
            base = 10 + rng.normal(0, 0.4, n)
            if s == faulty_service and k == kind:
                base[inject:] *= factor
            cols[f"{s}_{k}"] = base
    return pd.DataFrame(cols)


def synthetic_cases():
    """Enough varied cases for the scorer to learn 'the service whose mem or latency jumped'."""
    cases = []
    for i in range(36):
        system = "abc"[i % 3]
        service = CHAIN[i % 4]
        kind, fault = (("mem", "mem") if i % 2 else ("latency", "delay"))
        frame = metrics(service, kind, 6.0, seed=i)
        cases.append({"system": system, "service": service, "fault": fault,
                      "scores": scoring.service_scores(frame, 1_100, 60)})
    return cases


@pytest.fixture(scope="module")
def scorer():
    return scoring.Scorer(0).fit(synthetic_cases()).calibrate(synthetic_cases())


def test_scores_single_out_the_shifted_service_and_kind():
    scores = scoring.service_scores(metrics("database", "mem"), 1_100, 60)
    assert scores["database"]["mem"] > 20
    assert max(scores[s]["mem"] for s in CHAIN if s != "database") < 5


def test_columns_from_rcaeval_and_prometheus_parse_to_the_same_kinds():
    assert scoring.parse_column("cartservice_latency-90") == ("cartservice", "latency")
    assert scoring.parse_column("database_latency") == ("database", "latency")
    assert scoring.parse_column("main_cpu") is None


def test_scorer_names_the_faulty_service_and_fault(scorer):
    scores = scoring.service_scores(metrics("transaction-service", "mem", seed=99), 1_100, 60)
    service, fault, confidence, ranking = scorer.diagnose(scores)
    assert (service, fault) == ("transaction-service", "mem")
    assert 0 <= confidence <= 1 and ranking[0][0] == "transaction-service"


# ---------------------------------------------------------------- verdicts
def test_service_and_fault_agreeing_on_memory_pressure_is_agreement():
    v = op.judge("high_memory_usage", "transaction-service", "transaction-service", "mem", 0.8, 0.5)
    assert v["verdict"] == "agree" and v["basis"] == "service and fault"


def test_memory_diagnosis_with_a_different_fault_type_disagrees():
    v = op.judge("high_memory_usage", "transaction-service", "transaction-service", "cpu", 0.8, 0.5)
    assert v["verdict"] == "disagree" and "cpu" in v["reason"]


def test_a_different_service_disagrees_for_any_cause():
    for cause in ("db_disk_full", "db_connection_pool_exhaustion", "high_memory_usage"):
        v = op.judge(cause, "database", "frontend", "mem", 0.9, 0.5)
        assert v["verdict"] == "disagree", cause


def test_causes_with_no_matching_fault_type_are_checked_on_service_alone():
    v = op.judge("db_disk_full", "database", "database", "delay", 0.8, 0.5)
    assert v["verdict"] == "agree" and v["basis"] == "service only"


def test_low_confidence_abstains_rather_than_voting():
    v = op.judge("db_disk_full", "database", "frontend", "mem", 0.3, 0.5)
    assert v["verdict"] == "no_opinion"


# ---------------------------------------------------------------- endpoint
@pytest.fixture
def client(scorer, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setattr(svc, "model", scorer)
    monkeypatch.setattr(svc, "BASELINE_S", 100)
    monkeypatch.setattr(svc, "LEAD_S", 0)
    return TestClient(svc.app)


def serve(monkeypatch, frame):
    monkeypatch.setattr(svc, "fetch_metrics", lambda *a, **k: frame)


def test_endpoint_agrees_when_metrics_point_where_the_rules_did(client, monkeypatch):
    serve(monkeypatch, metrics("transaction-service", "mem", seed=7))
    body = client.post("/opinion", json={"root_cause": "high_memory_usage", "service": "transaction-service",
                                         "incident_start": 1_100}).json()
    assert body["verdict"] == "agree"
    assert body["ranking"][0]["service"] == "transaction-service"


def test_endpoint_disagrees_when_metrics_point_elsewhere(client, monkeypatch):
    serve(monkeypatch, metrics("database", "mem", seed=7))
    body = client.post("/opinion", json={"root_cause": "high_memory_usage", "service": "transaction-service",
                                         "incident_start": 1_100}).json()
    assert body["verdict"] == "disagree"


def test_endpoint_abstains_when_prometheus_has_nothing_or_fails(client, monkeypatch):
    serve(monkeypatch, pd.DataFrame(columns=["time"]))
    ask = {"root_cause": "db_disk_full", "service": "database", "incident_start": 1_100}
    assert client.post("/opinion", json=ask).json()["verdict"] == "no_opinion"

    def boom(*a, **k):
        raise RuntimeError("prometheus unreachable")
    monkeypatch.setattr(svc, "fetch_metrics", boom)
    assert client.post("/opinion", json=ask).json()["verdict"] == "no_opinion"


def test_endpoint_rejects_malformed_requests_and_reports_not_ready_without_a_model(client, monkeypatch):
    assert client.post("/opinion", json={"service": "database"}).status_code == 422
    monkeypatch.setattr(svc, "model", None)
    assert client.get("/ready").status_code == 503
    assert client.post("/opinion", json={"root_cause": "x", "service": "y", "incident_start": 1}).status_code == 503


def test_prometheus_client_builds_the_frame_the_scorer_expects(monkeypatch):
    def fake(base, query, start, end, step, timeout):
        if "memory_working_set" in query:
            return [{"metric": {"container": "database"}, "values": [[1000, "5"], [1015, "6"]]},
                    {"metric": {"container": "loadgen"}, "values": [[1000, "1"]]}]
        if "http_request_duration" in query:
            return [{"metric": {"service": "database"}, "values": [[1000, "0.1"], [1015, "NaN"]]}]
        return []
    monkeypatch.setattr(prom, "_get", fake)
    frame = prom.fetch_metrics("http://p", CHAIN, 0, 2000)
    assert set(frame.columns) == {"time", "database_mem", "database_latency"}
    assert frame.loc[frame.time == 1015, "database_mem"].item() == 6.0


def test_the_shipped_model_loads_and_finds_an_obvious_memory_fault(monkeypatch):
    import joblib
    path = HERE / "models" / "second_opinion.joblib"
    if not path.exists():
        pytest.skip("model not trained: run second-opinion/train.py")
    model = joblib.load(path)
    scores = scoring.service_scores(metrics("transaction-service", "mem", factor=20.0, seed=3), 1_100, 60)
    service, fault, confidence, _ = model.diagnose(scores)
    assert (service, fault) == ("transaction-service", "mem")


# ---------------------------------------------------------------- own-data additions
def test_a_cause_in_the_scorers_vocabulary_is_compared_on_fault_type_too():
    # a scorer trained on our incidents names our causes directly, so a disk claim can be refuted
    # by a scorer that finds the right service but a different cause
    v = op.judge("db_disk_full", "database", "database", "high_memory_usage", 0.9, 0.3,
                 vocabulary={"db_disk_full", "high_memory_usage"})
    assert v["verdict"] == "disagree" and "high_memory_usage" in v["reason"]
    v = op.judge("db_disk_full", "database", "database", "db_disk_full", 0.9, 0.3,
                 vocabulary={"db_disk_full"})
    assert v["verdict"] == "agree" and v["basis"] == "service and fault"
    # without the vocabulary (the RCAEval model) the old service-only behaviour is unchanged
    assert op.judge("db_disk_full", "database", "database", "mem", 0.9, 0.3)["basis"] == "service only"


def test_features_endpoint_returns_every_signal_and_needs_no_model(monkeypatch):
    from fastapi.testclient import TestClient
    seen = {}

    def fake(start, kinds, now=None):
        seen["kinds"] = kinds
        return {"database": {k: 0.0 for k in kinds}, "frontend": {k: 1.0 for k in kinds}}, None

    monkeypatch.setattr(svc, "model", None)
    monkeypatch.setattr(svc, "window_scores", fake)
    with TestClient(svc.app) as client:
        reply = client.post("/features", json={"incident_start": 1000}).json()
    assert set(reply["scores"]["database"]) == set(scoring.OWN_KINDS) and "disk" in seen["kinds"]
    monkeypatch.setattr(svc, "window_scores", lambda *a, **k: (None, "only 2 samples"))
    with TestClient(svc.app) as client:
        assert client.post("/features", json={"incident_start": 1000}).json() == {
            "scores": None, "reason": "only 2 samples"}
        assert client.post("/features", json={}).status_code == 422
