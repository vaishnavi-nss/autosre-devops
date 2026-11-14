import importlib.util
import json
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

DIRECTORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DIRECTORY))
spec = importlib.util.spec_from_file_location("dispatcher_under_test", DIRECTORY / "app.py")
svc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(svc)
from state import RemediationState

BODY = {"root_cause": svc.ROOT_CAUSE, "service": svc.TARGET, "confidence": 0.75}


@pytest.fixture
def configured(tmp_path, monkeypatch):
    monkeypatch.setattr(svc, "STATE", RemediationState(tmp_path / "state.sqlite3"))
    monkeypatch.setattr(svc, "ANSIBLE_DIR", DIRECTORY.parent / "ansible")
    monkeypatch.setattr(svc, "MAP_FILE", DIRECTORY.parent / "ansible/remediation_map.yml")
    monkeypatch.setattr(svc, "AUTO_REMEDIATE", True)
    return TestClient(svc.app)


def test_dry_run_never_executes_or_reserves(configured, monkeypatch):
    monkeypatch.setattr(svc, "AUTO_REMEDIATE", False)
    reserve = Mock(side_effect=AssertionError("dry-run must not reserve"))
    monkeypatch.setattr(svc.STATE, "reserve", reserve)
    assert configured.post("/dispatch", json=BODY).json()["status"] == "dry_run"
    reserve.assert_not_called()


@pytest.mark.parametrize("change", [
    {"service": "database"}, {"namespace": "default"}, {"root_cause": "db_disk_full"},
    {"root_cause": "high_memory_usage"}, {"confidence": 0.74}, {"confidence": 1.1},
    {"confidence": "NaN"}, {"confidence": "Infinity"}, {"confidence": None},
])
def test_unapproved_or_invalid_never_schedules(configured, monkeypatch, change):
    execute = Mock(side_effect=AssertionError("unapproved execution"))
    monkeypatch.setattr(svc, "execute", execute)
    assert configured.post("/dispatch", json={**BODY, **change}).json()["status"] == "escalated"
    execute.assert_not_called()


def test_tampered_mapping_is_rejected(configured, tmp_path, monkeypatch):
    mapping = tmp_path / "map.yml"
    mapping.write_text('remediations:\n  db_connection_pool_exhaustion:\n    playbook: clear_logs.yml\n')
    monkeypatch.setattr(svc, "MAP_FILE", mapping)
    assert configured.post("/dispatch", json=BODY).json()["status"] == "escalated"


def test_concurrent_requests_across_connections_have_one_winner(tmp_path):
    path = tmp_path / "shared.sqlite3"
    RemediationState(path).get("initialize")
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: RemediationState(path).reserve("pool", "tx")[0], range(16)))
    assert results.count("accepted") == 1
    assert results.count("cooldown") == 15
    assert RemediationState(path).reserve("other", "tx")[0] == "accepted"


def test_cooldown_survives_new_instance_and_expires(tmp_path, monkeypatch):
    path = tmp_path / "state.sqlite3"
    monkeypatch.setattr("state.time.time", lambda: 1000)
    state = RemediationState(path)
    _, job = state.reserve("pool", "tx")
    state.finish(job["id"], "succeeded", {})
    monkeypatch.setattr("state.time.time", lambda: 1599)
    assert RemediationState(path).reserve("pool", "tx")[0] == "cooldown"
    monkeypatch.setattr("state.time.time", lambda: 1600)
    assert RemediationState(path).reserve("pool", "tx")[0] == "accepted"


@pytest.mark.parametrize("state", ["failed", "running", "accepted"])
def test_failure_or_uncertain_execution_never_retries_after_expiry(tmp_path, monkeypatch, state):
    monkeypatch.setattr("state.time.time", lambda: 1000)
    store = RemediationState(tmp_path / "db")
    _, job = store.reserve("pool", "tx")
    store.finish(job["id"], state, {})
    monkeypatch.setattr("state.time.time", lambda: 2000)
    assert store.reserve("pool", "tx")[0] == "blocked"


def deployment(generation, job_id=None, **status):
    return NS(metadata=NS(generation=generation), spec=NS(replicas=1,
        template=NS(metadata=NS(annotations={"autosre.io/remediation-id": job_id}))),
        status=NS(**{**dict(observed_generation=generation, updated_replicas=1, replicas=1,
                           ready_replicas=1, available_replicas=1), **status}))


def test_old_ready_or_surge_deployment_is_not_completed_rollout():
    before = deployment(4)
    assert not svc.rollout_complete(before, deployment(4))
    assert not svc.rollout_complete(before, deployment(5, observed_generation=4))
    assert not svc.rollout_complete(before, deployment(5, replicas=2))
    assert svc.rollout_complete(before, deployment(5))


def mock_recovery(monkeypatch, healthy=True):
    monkeypatch.setattr(svc, "service_request", Mock(side_effect=[
        {"instance": "old-pod", "pool_exhausted": True},
        {"instance": "new-pod", "pool_exhausted": False, "pool_utilization": 0.1},
        {"status": "success" if healthy else "failed"},
    ]))


def test_actual_execution_requires_rollout_and_transaction_then_cools_down(configured, monkeypatch):
    def read_deployment():
        if not hasattr(read_deployment, "called"):
            read_deployment.called = True
            return deployment(4)
        return deployment(5, current_job[0])
    current_job = []
    original = svc.STATE.reserve
    def reserve(*args, **kwargs):
        status, job = original(*args, **kwargs)
        current_job[:] = [job["id"]]
        return status, job
    monkeypatch.setattr(svc.STATE, "reserve", reserve)
    monkeypatch.setattr(svc, "deployment", read_deployment)
    process = Mock(return_value=NS(returncode=0, stdout="rollout completed", stderr=""))
    monkeypatch.setattr(svc.subprocess, "run", process)
    mock_recovery(monkeypatch)
    response = configured.post("/dispatch", json=BODY)
    assert response.status_code == 202
    job_id = response.json()["job_id"]
    job = configured.get("/remediations/" + job_id).json()
    assert job["status"] == "succeeded"
    assert job["details"]["recovery"]["instance"] == "new-pod"
    assert configured.post("/dispatch", json=BODY).json()["status"] == "cooldown"
    assert process.call_count == 1
    command = process.call_args.args[0]
    assert command[0] == "ansible-playbook" and "playbooks/restart_service.yml" in command
    assert json.loads(command[-1])["target_service"] == "transaction-service"


def test_ansible_failure_escalates_and_locks_future_requests(configured, monkeypatch):
    monkeypatch.setattr(svc, "deployment", lambda: deployment(4))
    monkeypatch.setattr(svc, "service_request", lambda *args: {"pool_exhausted": True})
    process = Mock(return_value=NS(returncode=2, stdout="RBAC denied", stderr=""))
    monkeypatch.setattr(svc.subprocess, "run", process)
    accepted = configured.post("/dispatch", json=BODY).json()
    job = configured.get("/remediations/" + accepted["job_id"]).json()
    assert job["status"] == "failed" and job["details"]["action"] == "escalate_to_human"
    assert configured.post("/dispatch", json=BODY).json()["status"] == "escalated"
    assert process.call_count == 1


def test_healthy_target_is_not_restarted(configured, monkeypatch):
    monkeypatch.setattr(svc, "deployment", lambda: deployment(4))
    monkeypatch.setattr(svc, "service_request", lambda *args: {"pool_exhausted": False})
    process = Mock(side_effect=AssertionError("healthy target restart"))
    monkeypatch.setattr(svc.subprocess, "run", process)
    job = configured.post("/dispatch", json=BODY).json()
    assert configured.get("/remediations/" + job["job_id"]).json()["status"] == "not_needed"
    process.assert_not_called()


def test_rollout_without_application_recovery_is_failed(configured, monkeypatch):
    store = svc.STATE
    _, job = store.reserve(svc.ROOT_CAUSE, svc.TARGET)
    reads = iter([deployment(4), deployment(5, job["id"])])
    monkeypatch.setattr(svc, "deployment", lambda: next(reads))
    monkeypatch.setattr(svc.subprocess, "run", Mock(return_value=NS(returncode=0, stdout="ready", stderr="")))
    monkeypatch.setattr(svc, "service_request", lambda *a: {"instance": "old-pod", "pool_exhausted": True})
    clock = iter([0, 1, 31])
    monkeypatch.setattr(svc.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(svc.time, "sleep", lambda _: None)
    svc.execute(job["id"])
    assert store.get(job["id"])["status"] == "failed"
    assert store.reserve(svc.ROOT_CAUSE, svc.TARGET)[0] == "blocked"


def test_metrics_export_decisions_and_outcomes(configured):
    result = configured.get("/metrics")
    assert result.status_code == 200
    for name in ("remediation_success_total", "remediation_failure_total", "remediation_cooldown_total"):
        assert name in result.text


def legacy_history(path, rows):
    """Reproduce the six-column schema already deployed before telemetry support."""
    with sqlite3.connect(path) as connection:
        connection.execute("""CREATE TABLE jobs (id TEXT PRIMARY KEY, root_cause TEXT NOT NULL,
            service TEXT NOT NULL, requested_at REAL NOT NULL, status TEXT NOT NULL, details TEXT NOT NULL)""")
        for job_id, requested, status, details in rows:
            connection.execute("INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?)",
                               (job_id, svc.ROOT_CAUSE, svc.TARGET, requested, status, json.dumps(details)))


def test_latest_empty_does_not_create_database_or_dispatch(configured, monkeypatch):
    monkeypatch.setattr(svc.STATE, "connect", Mock(side_effect=AssertionError("Read endpoint must not connect for writes")))
    monkeypatch.setattr(svc.STATE, "reserve", Mock(side_effect=AssertionError("Read endpoint must not reserve")))
    monkeypatch.setattr(svc, "execute", Mock(side_effect=AssertionError("Read endpoint must not execute")))
    before = configured.get("/metrics").content
    response = configured.get("/remediations/latest")
    assert response.status_code == 200  # Static route must precede /{job_id}.
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["status"] == "empty"
    assert response.json()["remediation"] is None
    assert response.json()["settings"]["auto_remediate"] is True
    assert configured.get("/metrics").content == before
    assert not svc.STATE.path.exists()


def test_latest_legacy_completed_job_is_read_only_and_confidence_unknown(configured, monkeypatch):
    proof = {"playbook": "restart_service.yml", "generation": 9,
             "recovery": {"instance": "recovered-pod", "pool_utilization": 0.1}}
    legacy_history(svc.STATE.path, [("completed", 1000, "succeeded", proof)])
    before = svc.STATE.path.read_bytes()
    monkeypatch.setattr(svc.STATE, "connect", Mock(side_effect=AssertionError("No migration on GET")))
    response = configured.get("/remediations/latest")
    job = response.json()["remediation"]
    assert job["id"] == "completed" and job["status"] == "succeeded"
    assert job["confidence"] is None and job["playbook"] == proof["playbook"]
    assert job["details"] == proof
    assert svc.STATE.path.read_bytes() == before
    with sqlite3.connect(svc.STATE.path) as connection:
        assert len(connection.execute("PRAGMA table_info(jobs)").fetchall()) == 6


def test_latest_selects_newest_request_and_breaks_timestamp_ties(configured):
    legacy_history(svc.STATE.path, [("old-completed", 1000, "succeeded", {}),
                                   ("new-running", 2000, "running", {}),
                                   ("newest", 2000, "failed", {"reason": "Recovery failed"})])
    assert configured.get("/remediations/latest").json()["remediation"]["id"] == "newest"
    assert configured.get("/remediations/old-completed").json()["status"] == "succeeded"


def test_writer_migrates_legacy_history_without_inventing_metadata(configured, monkeypatch):
    old_proof = {"playbook": "restart_service.yml", "generation": 9}
    legacy_history(svc.STATE.path, [("historical", 1000, "succeeded", old_proof)])
    monkeypatch.setattr("state.time.time", lambda: 2000)
    status, job = svc.STATE.reserve(svc.ROOT_CAUSE, svc.TARGET, confidence=0.87, playbook=svc.PLAYBOOK)
    assert status == "accepted"
    svc.STATE.finish(job["id"], "running", {})
    svc.STATE.finish(job["id"], "succeeded", {"generation": 10})
    new = svc.STATE.latest()
    assert new["confidence"] == 0.87 and new["playbook"] == svc.PLAYBOOK
    historical = svc.STATE.get("historical")
    assert historical["confidence"] is None and historical["details"] == old_proof


def test_duplicate_does_not_overwrite_original_diagnosis_metadata(configured):
    _, job = svc.STATE.reserve(svc.ROOT_CAUSE, svc.TARGET, confidence=0.78, playbook=svc.PLAYBOOK)
    svc.STATE.finish(job["id"], "succeeded", {})
    status, previous = svc.STATE.reserve(svc.ROOT_CAUSE, svc.TARGET, confidence=1.0, playbook="different.yml")
    assert status == "cooldown" and previous["id"] == job["id"]
    assert svc.STATE.latest()["confidence"] == 0.78
    assert svc.STATE.latest()["playbook"] == svc.PLAYBOOK


def test_concurrent_legacy_migration_does_not_change_reservation_behavior(tmp_path):
    path = tmp_path / "old.sqlite3"
    legacy_history(path, [])
    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(lambda _: RemediationState(path).reserve(
            svc.ROOT_CAUSE, svc.TARGET, confidence=0.79, playbook=svc.PLAYBOOK)[0], range(16)))
    assert outcomes.count("accepted") == 1 and outcomes.count("cooldown") == 15
    assert RemediationState(path).latest()["confidence"] == 0.79


@pytest.mark.parametrize("status,now,active,review,remaining", [
    ("succeeded", 1100, True, False, 500),
    ("succeeded", 1600, False, False, 0),
    ("failed", 1100, False, True, 0),
    ("running", 1100, True, False, 500),
    ("running", 1600, False, True, 0),
    ("accepted", 1600, False, True, 0),
    ("not_needed", 1100, True, False, 500),
])
def test_read_only_cooldown_matches_existing_lockout_semantics(configured, monkeypatch, status, now, active, review, remaining):
    legacy_history(svc.STATE.path, [("job", 1000, status, {})])
    monkeypatch.setattr(svc.time, "time", lambda: now)
    assert configured.get("/remediations/latest").json()["cooldown"] == {
        "active": active, "remaining_seconds": remaining, "operator_review_required": review}


def test_dispatch_records_received_confidence_without_changing_execution(configured, monkeypatch):
    monkeypatch.setattr(svc, "deployment", lambda: deployment(4))
    monkeypatch.setattr(svc, "service_request", lambda *args: {"pool_exhausted": False})
    response = configured.post("/dispatch", json={**BODY, "confidence": 0.86})
    assert response.status_code == 202
    job = configured.get("/remediations/latest").json()["remediation"]
    assert job["status"] == "not_needed"
    assert job["confidence"] == 0.86 and job["playbook"] == svc.PLAYBOOK


def test_existing_database_without_jobs_table_is_empty_and_unchanged(configured):
    with sqlite3.connect(svc.STATE.path) as connection:
        connection.execute("CREATE TABLE unrelated (id INTEGER)")
    before = svc.STATE.path.read_bytes()
    assert configured.get("/remediations/latest").json()["status"] == "empty"
    assert svc.STATE.path.read_bytes() == before


def test_corrupt_history_is_unavailable_not_empty(configured):
    svc.STATE.path.write_bytes(b"not a SQLite database")
    response = configured.get("/remediations/latest")
    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"
