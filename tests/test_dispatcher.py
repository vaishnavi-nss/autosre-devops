"""The remediation dispatcher: policy, guardrails, execution and verification.

Nothing here touches a cluster or runs ansible. Kubernetes is replaced by a fake and `subprocess.run` by a stub, so these tests
prove the dispatcher's decisions and bookkeeping. They do not prove that a playbook works against a real cluster; that is shown
by the live scenarios (docs/completion).
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
os.environ["ANSIBLE_DIR"] = str(ROOT / "ansible")

_spec = importlib.util.spec_from_file_location("dispatcher_app", ROOT / "remediation-dispatcher" / "app.py")
disp = importlib.util.module_from_spec(_spec)
sys.modules["dispatcher_app"] = disp
_spec.loader.exec_module(disp)

client = TestClient(disp.app)

AGREE = {"verdict": "agree"}
POOL = {"root_cause": "db_connection_pool_exhaustion", "confidence": 0.9, "service": "transaction-service", "second_opinion": AGREE}
MEM = {"root_cause": "high_memory_usage", "confidence": 0.85, "service": "transaction-service", "second_opinion": AGREE}
DISK = {"root_cause": "db_disk_full", "confidence": 0.8, "service": "database", "second_opinion": AGREE}


class FakeCluster:
    """A deployment that behaves like Kubernetes: a restart bumps the generation, a scale changes replicas."""

    def __init__(self):
        self.state = {"generation": 1, "observed": 1, "replicas": 1, "updated": 1, "ready": 1, "available": 1}
        self.scaled_to: list[int] = []
        self.http: dict[str, tuple[int, str]] = {}
        self.unreadable = False
        self.settle = True            # False: the rollout never completes

    def deployment(self, name, namespace):
        if self.unreadable:
            raise RuntimeError("forbidden")
        return dict(self.state)

    def scale(self, name, namespace, replicas):
        self.scaled_to.append(replicas)
        self.state.update(replicas=replicas, updated=replicas, ready=replicas, available=replicas)

    def get(self, url, timeout=4.0):
        for suffix, reply in self.http.items():
            if url.endswith(suffix):
                return reply
        return 200, "ok"

    # what ansible would have done
    def restart(self):
        s = self.state
        s["generation"] += 1
        s["observed"] = s["generation"] if self.settle else s["generation"] - 1

    def scale_out(self, n):
        s = self.state
        if s["replicas"] != n:                                  # scaling to the count it already has changes nothing
            s["generation"] += 1
        s.update(replicas=n, observed=s["generation"], updated=n if self.settle else 1, ready=n if self.settle else 1,
                 available=n if self.settle else 1)


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    """Fresh state, live execution on, a fake cluster, ansible present, inline launcher and an instant clock."""
    disp._executions.clear(); disp._cooldowns.clear(); disp._inflight.clear()
    monkeypatch.setenv("AUTO_REMEDIATE", "true")
    monkeypatch.setenv("COOLDOWN_SECONDS", "600")
    monkeypatch.setenv("VERIFY_TIMEOUT_SECONDS", "0")
    monkeypatch.delenv("SECOND_OPINION_POLICY", raising=False)
    fake = FakeCluster()
    monkeypatch.setattr(disp, "cluster", fake)
    monkeypatch.setattr(disp, "_ansible_available", lambda: True)
    monkeypatch.setattr(disp, "launcher", lambda record: disp.run_execution(record, sleep=lambda s: None))
    ran: list[list[str]] = []

    def fake_run(cmd, **kw):
        ran.append(cmd)
        play = cmd[3]
        if "restart" in play:
            fake.restart()
        elif "scale" in play:
            fake.scale_out(int(yaml_vars(cmd)["replicas"]))
            if yaml_vars(cmd).get("replace_pods") and fake.__dict__.get("roll", True):
                fake.restart()                                  # the playbook also rolls every pod
        return subprocess.CompletedProcess(cmd, fake.__dict__.get("rc", 0), "ok", "")

    monkeypatch.setattr(disp.subprocess, "run", fake_run)
    fake.ran = ran
    return fake


def yaml_vars(cmd):
    import json
    return json.loads(cmd[cmd.index("--extra-vars") + 1])


# ------------------------------------------------------------------------------------------------ policy
def test_all_three_approved_playbooks_are_allowed():
    assert disp.ALLOWED_PLAYBOOKS == {"restart_service.yml", "scale_replicas.yml", "clear_logs.yml"}
    mapping = yaml.safe_load((ROOT / "ansible" / "remediation_map.yml").read_text())["remediations"]
    for cause, entry in mapping.items():
        spec = disp.POLICY[entry["playbook"]]
        assert entry["extra_vars"]["target_service"] in spec["targets"], cause
        assert set(entry["extra_vars"]) <= spec["vars"], cause


@pytest.mark.parametrize("payload,playbook,target", [(POOL, "restart_service.yml", "transaction-service"),
                                                      (MEM, "scale_replicas.yml", "transaction-service"),
                                                      (DISK, "clear_logs.yml", "database")])
def test_each_known_cause_maps_to_its_playbook(payload, playbook, target):
    d = disp.decide(payload)
    assert d["status"] == "ready" and d["playbook"] == playbook and d["target_service"] == target and d["namespace"] == "autosre"


def test_dry_run_is_the_default_and_runs_nothing(monkeypatch, isolated):
    monkeypatch.delenv("AUTO_REMEDIATE")
    r = client.post("/dispatch", json=POOL)
    assert r.status_code == 200 and r.json()["status"] == "dry_run"
    assert isolated.ran == [] and not disp._executions


@pytest.mark.parametrize("payload,reason", [
    ({}, "Missing root cause"),
    ({"root_cause": "db_disk_full"}, "Missing or invalid confidence"),
    ({"root_cause": "db_disk_full", "confidence": "x"}, "Missing or invalid confidence"),
    ({"root_cause": "db_disk_full", "confidence": 1.5}, "between 0 and 1"),
    ({"root_cause": "db_disk_full", "confidence": 0.74, "second_opinion": AGREE}, "below threshold"),
    ({"root_cause": "something_new", "confidence": 0.99, "second_opinion": AGREE}, "No remediation mapping"),
])
def test_unknown_unsure_or_malformed_diagnoses_escalate(payload, reason, isolated):
    d = disp.decide(payload)
    assert d["status"] == "escalated" and reason in d["reason"]
    assert client.post("/dispatch", json=payload).json()["status"] == "escalated" and isolated.ran == []


def test_a_nan_confidence_escalates():
    d = disp.decide({"root_cause": "db_disk_full", "confidence": float("nan"), "second_opinion": AGREE})
    assert d["status"] == "escalated" and "between 0 and 1" in d["reason"]


def test_the_threshold_is_inclusive_at_0_75():
    assert disp.decide({**DISK, "confidence": 0.75})["status"] == "ready"
    assert disp.decide({**DISK, "confidence": 0.7499})["status"] == "escalated"


@pytest.mark.parametrize("so", [None, {"verdict": "disagree"}, {"verdict": "no_opinion"}, {"verdict": "unavailable"}, "disagree"])
def test_agreement_is_required_by_default(so):
    d = disp.decide({**POOL, "second_opinion": so})
    assert d["status"] == "escalated" and "policy requires agree" in d["reason"]


def test_policy_not_disagree_and_ignore(monkeypatch):
    monkeypatch.setenv("SECOND_OPINION_POLICY", "not_disagree")
    assert disp.decide({**POOL, "second_opinion": {"verdict": "no_opinion"}})["status"] == "ready"
    assert disp.decide({**POOL, "second_opinion": {"verdict": "disagree"}})["status"] == "escalated"
    monkeypatch.setenv("SECOND_OPINION_POLICY", "ignore")
    assert disp.decide({**POOL, "second_opinion": None})["status"] == "ready"


def test_a_diagnosis_for_the_wrong_service_is_refused():
    d = disp.decide({**POOL, "service": "database"})
    assert d["status"] == "escalated" and "differs from the playbook target" in d["reason"]


@pytest.mark.parametrize("entry,reason", [
    ({"playbook": "delete_everything.yml", "extra_vars": {"target_service": "database"}}, "not approved"),
    ({"playbook": "restart_service.yml", "extra_vars": {"target_service": "database"}}, "not an approved target"),
    ({"playbook": "restart_service.yml", "extra_vars": {"target_service": "transaction-service", "namespace": "kube-system"}}, "not allowed"),
    ({"playbook": "scale_replicas.yml", "extra_vars": {"target_service": "transaction-service", "replicas": 50}}, "replicas must be"),
    ({"playbook": "scale_replicas.yml", "extra_vars": {"target_service": "transaction-service", "replicas": 0}}, "replicas must be"),
    ({"playbook": "scale_replicas.yml", "extra_vars": {"target_service": "transaction-service", "replicas": "many"}}, "not an integer"),
    ({"playbook": "scale_replicas.yml", "extra_vars": {"target_service": "transaction-service", "replicas": 3, "replace_pods": "yes"}}, "true or false"),
])
def test_a_tampered_map_cannot_widen_what_runs(entry, reason, monkeypatch, tmp_path):
    bad = tmp_path / "remediation_map.yml"
    bad.write_text(yaml.safe_dump({"remediations": {"high_memory_usage": entry}}))
    monkeypatch.setattr(disp, "MAP_FILE", bad)
    d = disp.decide(MEM)
    assert d["status"] == "escalated" and reason in d["reason"]


# ------------------------------------------------------------------------------------------------ execution
def test_restart_succeeds_only_after_the_rollout_is_verified(isolated):
    r = client.post("/dispatch", json=POOL)
    assert r.status_code == 202 and r.json()["status"] == "accepted"
    rec = client.get("/executions/" + r.json()["execution_id"]).json()
    assert rec["status"] == "succeeded" and rec["recovered"] is True
    assert rec["before"]["generation"] == 1 and rec["after"]["generation"] == 2
    cmd = isolated.ran[0]
    assert cmd[:2] == ["ansible-playbook", "-i"] and cmd[3] == "playbooks/restart_service.yml"
    assert yaml_vars(cmd) == {"target_service": "transaction-service", "k8s_namespace": "autosre"}
    assert [s["step"] for s in rec["steps"]] == ["preflight", "ansible", "verify", "recovered"]


def test_a_rollout_that_never_completes_is_a_failure_not_a_success(isolated):
    isolated.settle = False
    rec = disp.run_execution(_accept(POOL), sleep=lambda s: None)
    assert rec["status"] == "verification_failed" and rec["recovered"] is False and rec["escalate"] is True
    assert "rollout not complete" in rec["reason"]


def test_a_service_that_is_up_but_unhealthy_is_not_recovered(isolated):
    isolated.http["/health"] = (503, "down")
    rec = disp.run_execution(_accept(POOL), sleep=lambda s: None)
    assert rec["status"] == "verification_failed" and "/health answered 503" in rec["reason"]


def test_scale_out_is_verified_and_a_failed_one_is_rolled_back(isolated):
    rec = disp.run_execution(_accept(MEM), sleep=lambda s: None)
    assert rec["status"] == "succeeded" and rec["after"]["replicas"] == 3 and isolated.scaled_to == []
    disp._cooldowns.clear(); disp._inflight.clear()
    isolated.state.update(replicas=1, updated=1, ready=1, available=1)
    isolated.settle = False
    rec = disp.run_execution(_accept(MEM), sleep=lambda s: None)
    assert rec["status"] == "verification_failed" and rec["rolled_back_to"] == 1 and isolated.scaled_to == [1]


def test_rollback_can_be_turned_off(isolated, monkeypatch):
    monkeypatch.setenv("ROLLBACK_ON_FAILURE", "false")
    isolated.settle = False
    rec = disp.run_execution(_accept(MEM), sleep=lambda s: None)
    assert rec["status"] == "verification_failed" and "rolled_back_to" not in rec and isolated.scaled_to == []


def test_clear_logs_is_verified_against_the_disk_metric(isolated):
    isolated.http["/metrics"] = (200, 'db_disk_usage_ratio{service="database"} 0.42\n')
    rec = disp.run_execution(_accept(DISK), sleep=lambda s: None)
    assert rec["status"] == "succeeded" and "disk ratio 0.42" in rec["reason"]
    assert yaml_vars(isolated.ran[-1]) == {"target_service": "database", "k8s_namespace": "autosre"}
    disp._cooldowns.clear(); disp._inflight.clear()
    isolated.http["/metrics"] = (200, 'db_disk_usage_ratio{service="database"} 0.95\n')
    rec = disp.run_execution(_accept(DISK), sleep=lambda s: None)
    assert rec["status"] == "verification_failed" and "still 0.95" in rec["reason"]
    disp._cooldowns.clear(); disp._inflight.clear()
    isolated.http["/metrics"] = (500, "boom")
    assert disp.run_execution(_accept(DISK), sleep=lambda s: None)["reason"] == "disk metric unreadable"


def test_ansible_failure_and_timeout_are_recorded_and_escalated(isolated, monkeypatch):
    isolated.rc = 2
    rec = disp.run_execution(_accept(POOL), sleep=lambda s: None)
    assert rec["status"] == "failed" and rec["return_code"] == 2
    disp._cooldowns.clear(); disp._inflight.clear()

    def boom(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 1)
    monkeypatch.setattr(disp.subprocess, "run", boom)
    rec = disp.run_execution(_accept(POOL), sleep=lambda s: None)
    assert rec["status"] == "failed" and "timed out" in rec["reason"]


def test_a_failed_preflight_stops_before_anything_runs_and_sets_no_cooldown(isolated):
    isolated.unreadable = True
    r = client.post("/dispatch", json=POOL)
    rec = client.get("/executions/" + r.json()["execution_id"]).json()
    assert rec["status"] == "preflight_failed" and isolated.ran == [] and rec["escalate"] is True
    isolated.unreadable = False
    assert client.post("/dispatch", json=POOL).status_code == 202          # nothing ran, so nothing is cooling down


def test_missing_ansible_is_a_failed_preflight(isolated, monkeypatch):
    monkeypatch.setattr(disp, "_ansible_available", lambda: False)
    rec = disp.run_execution(_accept(POOL), sleep=lambda s: None)
    assert rec["status"] == "preflight_failed" and "not installed" in rec["reason"] and isolated.ran == []


# ------------------------------------------------------------------------------------------------ cooldown and duplicates
def test_cooldown_blocks_a_repeat_after_a_run(isolated):
    assert client.post("/dispatch", json=POOL).status_code == 202
    again = client.post("/dispatch", json=POOL).json()
    assert again["status"] == "skipped" and again["reason"] == "cooldown" and again["cooldown_remaining_s"] > 0
    assert len(isolated.ran) == 1
    assert client.post("/dispatch", json=MEM).status_code == 202            # a different cause is not blocked
    state = client.get("/state").json()
    assert {c["root_cause"] for c in state["cooldowns"]} == {"db_connection_pool_exhaustion", "high_memory_usage"}


def test_a_run_in_progress_blocks_a_duplicate(monkeypatch, isolated):
    monkeypatch.setattr(disp, "launcher", lambda record: None)             # the run is "still going"
    assert client.post("/dispatch", json=POOL).status_code == 202
    dup = client.post("/dispatch", json=POOL).json()
    assert dup["status"] == "skipped" and "duplicate" in dup["reason"] and isolated.ran == []
    assert client.get("/state").json()["in_flight"] == [["db_connection_pool_exhaustion", "transaction-service"]]


def test_cooldown_expires(isolated, monkeypatch):
    client.post("/dispatch", json=POOL)
    key = ("db_connection_pool_exhaustion", "transaction-service")
    disp._cooldowns[key] = 0
    assert client.post("/dispatch", json=POOL).status_code == 202


# ------------------------------------------------------------------------------------------------ tracking and metrics
def test_executions_are_listed_newest_first_and_a_stranger_is_404(isolated):
    client.post("/dispatch", json=POOL); client.post("/dispatch", json=MEM)
    items = client.get("/executions").json()["executions"]
    assert [i["root_cause"] for i in items] == ["high_memory_usage", "db_connection_pool_exhaustion"]
    assert client.get("/executions/nope").status_code == 404


def test_metrics_are_exported(isolated):
    client.post("/dispatch", json=POOL)
    text = client.get("/metrics").text
    for name in ("remediation_decisions_total", "remediation_executions_total", "remediation_skipped_total", "remediation_in_progress"):
        assert name in text
    h = client.get("/health").json()
    assert h["auto_remediate"] is True and h["approved_playbooks"] == sorted(disp.POLICY)


# ------------------------------------------------------------------------------------------------ packaging and manifests
def test_the_image_builds_from_a_clean_clone():
    docker = (ROOT / "remediation-dispatcher" / "Dockerfile").read_text()
    ignored = [l.strip("/ \n") for l in (ROOT / ".gitignore").read_text().splitlines() if l.startswith("/")]
    for path in ignored:
        assert f"COPY {path}/" not in docker, f"Dockerfile copies the git-ignored {path}"
    assert "ansible-galaxy collection install" in docker and "USER appuser" in docker
    assert "docker build -t autosre/remediation-dispatcher:dev -f remediation-dispatcher/Dockerfile ." in (ROOT / "scripts" / "build-images.sh").read_text()
    assert "remediation-dispatcher" in (ROOT / "scripts" / "deploy.sh").read_text()


def test_the_manifest_is_safe_by_default_and_grants_what_clear_logs_needs():
    docs = list(yaml.safe_load_all((ROOT / "k8s" / "remediation-dispatcher.yaml").read_text()))
    deploy = next(d for d in docs if d and d["kind"] == "Deployment")
    env = {e["name"]: e["value"] for e in deploy["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["AUTO_REMEDIATE"] == "false" and env["MIN_CONFIDENCE"] == "0.75" and env["SECOND_OPINION_POLICY"] == "agree"
    role = next(d for d in docs if d and d["kind"] == "Role")
    granted = {(g, r, v) for rule in role["rules"] for g in rule["apiGroups"] for r in rule["resources"] for v in rule["verbs"]}
    assert ("", "pods/exec", "create") in granted and ("", "pods/exec", "get") in granted and ("", "pods", "list") in granted
    assert ("apps", "deployments/scale", "update") in granted
    assert not any(v in ("delete", "*") for _, _, v in granted)             # nothing can be deleted through the API


def test_the_playbooks_are_valid_yaml_with_guards_and_clear_logs_is_real():
    for name in ("restart_service.yml", "scale_replicas.yml", "clear_logs.yml"):
        play = yaml.safe_load((ROOT / "ansible" / "playbooks" / name).read_text())[0]
        tasks = [next(iter(t)) for t in play["tasks"]]
        assert "name" in play["tasks"][0] and "ansible.builtin.assert" in play["tasks"][0], name        # the target guard comes first
        assert "allowed_services" in play["vars"] and play["vars"]["k8s_namespace"] == "autosre"
    text = (ROOT / "ansible" / "playbooks" / "clear_logs.yml").read_text()
    assert "NOT IMPLEMENTED" not in text and "-delete" in text and "-maxdepth 1" in text and "-name *.log" in text
    assert "approved_log_dir" in text and "ansible.builtin.shell" not in text and "ansible.builtin.command" not in text
    restart = (ROOT / "ansible" / "playbooks" / "restart_service.yml").read_text()
    assert "observedGeneration" in restart and "updatedReplicas" in restart and "availableReplicas" in restart


def _accept(payload):
    """Register an execution the way /dispatch does, without the HTTP layer."""
    d = disp.decide(payload)
    assert d["status"] == "ready", d
    record = {"id": "t" + str(len(disp._executions)), "status": "running", "started_at": disp._now(), "steps": [], "recovered": None,
              **{k: d[k] for k in ("root_cause", "confidence", "playbook", "target_service", "namespace", "extra_vars", "second_opinion")}}
    disp._executions[record["id"]] = record
    disp._inflight.add((record["root_cause"], record["target_service"]))
    disp.in_progress.inc()
    return record


def test_the_deployments_view_reports_rollout_progress(isolated):
    isolated.state.update(generation=3, observed=3, replicas=3, updated=2, ready=1, available=1)
    body = client.get("/deployments").json()
    tx = next(d for d in body["deployments"] if d["name"] == "transaction-service")
    assert body["namespace"] == "autosre" and tx["rolling_out"] is True and tx["ready"] == 1 and tx["replicas"] == 3
    isolated.state.update(updated=3, ready=3, available=3)
    assert next(d for d in client.get("/deployments").json()["deployments"] if d["name"] == "database")["rolling_out"] is False
    isolated.unreadable = True
    assert "error" in client.get("/deployments").json()["deployments"][0]


def test_the_non_root_user_can_write_ansibles_temp_directory():
    """Found on the real cluster: /tmp/.ansible was left root-owned by the build, so ansible-playbook failed as appuser."""
    docker = (ROOT / "remediation-dispatcher" / "Dockerfile").read_text()
    line = next(l for l in docker.splitlines() if l.startswith("RUN useradd"))
    assert "/tmp/.ansible" in line and "chown -R appuser" in line
    assert docker.index("RUN useradd") > docker.index("ansible-galaxy collection install") and docker.index("USER appuser") > docker.index("RUN useradd")


# ------------------------------------------------------------------------------------------------ real cluster access (REST)
def test_the_real_cluster_class_calls_the_api_server_with_the_service_account_token(monkeypatch, tmp_path):
    """Found on the real cluster: the kubernetes client 36.0.0 sent requests as system:anonymous; a raw call with the token worked."""
    (tmp_path / "token").write_text("sa-token\n")
    (tmp_path / "ca.crt").write_text("")
    monkeypatch.setattr(disp, "SA_DIR", tmp_path)
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.96.0.1")
    monkeypatch.setenv("KUBERNETES_SERVICE_PORT", "443")
    import ssl
    monkeypatch.setattr(ssl, "create_default_context", lambda cafile=None: object())
    seen = []

    class Resp:
        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            import json
            return json.dumps(self.body).encode()

    def fake_urlopen(req, context=None, timeout=0):
        seen.append((req.get_method(), req.full_url, dict(req.header_items()), req.data))
        if req.get_method() == "GET":
            return Resp({"metadata": {"generation": 4}, "spec": {"replicas": 3},
                         "status": {"observedGeneration": 4, "updatedReplicas": 3, "readyReplicas": 2, "availableReplicas": 2}})
        return Resp({})

    monkeypatch.setattr(disp.urllib.request, "urlopen", fake_urlopen)
    real = disp.Cluster()
    assert real.deployment("transaction-service", "autosre") == {"generation": 4, "observed": 4, "replicas": 3, "updated": 3, "ready": 2, "available": 2}
    real.scale("transaction-service", "autosre", 1)
    method, url, headers, data = seen[0]
    assert (method, url) == ("GET", "https://10.96.0.1:443/apis/apps/v1/namespaces/autosre/deployments/transaction-service")
    assert headers["Authorization"] == "Bearer sa-token"
    method, url, headers, data = seen[1]
    assert method == "PATCH" and url.endswith("/deployments/transaction-service/scale") and headers["Content-type"] == "application/merge-patch+json"
    assert data == b'{"spec": {"replicas": 1}}'


def test_a_missing_cluster_or_an_api_error_is_a_clear_failure_not_a_crash(monkeypatch):
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    import pytest as _pytest
    with _pytest.raises(RuntimeError, match="not running inside a cluster"):
        disp.Cluster().deployment("x", "autosre")


def test_the_kubernetes_client_ansible_uses_is_pinned():
    reqs = (ROOT / "remediation-dispatcher" / "requirements.txt").read_text()
    assert "kubernetes==31.0.0" in reqs and "kubernetes>=" not in reqs


# ---------------------------------------------------------------- history survives a restart
def _record(rid, finished_ago_s, status="succeeded", cause="db_connection_pool_exhaustion", target="transaction-service"):
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    return {"id": rid, "status": status, "started_at": (now - timedelta(seconds=finished_ago_s + 30)).isoformat(timespec="seconds"),
            "finished_at": (now - timedelta(seconds=finished_ago_s)).isoformat(timespec="seconds"), "playbook": "restart_service.yml",
            "root_cause": cause, "target_service": target, "steps": [], "recovered": status == "succeeded"}


def test_history_and_active_cooldowns_are_reloaded_after_a_restart(tmp_path, monkeypatch):
    import json
    log_file = tmp_path / "executions.jsonl"
    old, fresh, failed_preflight = _record("old", 5000), _record("fresh", 100), _record("pf", 50, status="preflight_failed", cause="db_disk_full", target="database")
    log_file.write_text("\n".join([json.dumps(old), "not json", json.dumps(fresh), json.dumps({"no": "id"}), json.dumps(failed_preflight)]) + "\n", encoding="utf-8")
    monkeypatch.setenv("EXEC_LOG", str(log_file))
    assert disp._load_history(disp.settings()) == 3
    assert list(disp._executions) == ["old", "fresh", "pf"]                                   # history is back, oldest first, junk skipped
    assert ("db_connection_pool_exhaustion", "transaction-service") in disp._cooldowns         # finished 100 s ago, cooldown is 600 s
    remaining = disp._cooldowns[("db_connection_pool_exhaustion", "transaction-service")] - disp.time.monotonic()
    assert 450 < remaining <= 500
    assert ("db_disk_full", "database") not in disp._cooldowns                                  # a preflight failure never started a cooldown


def test_a_restored_cooldown_blocks_the_same_fix_running_again(tmp_path, monkeypatch, isolated):
    import json
    log_file = tmp_path / "executions.jsonl"
    log_file.write_text(json.dumps(_record("fresh", 60)) + chr(10), encoding="utf-8")
    monkeypatch.setenv("EXEC_LOG", str(log_file))
    disp._load_history(disp.settings())
    again = client.post("/dispatch", json=POOL).json()                      # the same pool incident, right after a "restart"
    assert again["status"] == "skipped" and again["reason"] == "cooldown" and isolated.ran == []
    assert client.post("/dispatch", json=MEM).status_code == 202            # a different cause is not blocked


def test_with_no_log_configured_or_a_missing_file_nothing_is_loaded(tmp_path, monkeypatch):
    monkeypatch.delenv("EXEC_LOG", raising=False)
    assert disp._load_history(disp.settings()) == 0
    monkeypatch.setenv("EXEC_LOG", str(tmp_path / "absent.jsonl"))
    assert disp._load_history(disp.settings()) == 0 and not disp._executions


def test_the_manifest_gives_the_history_a_persistent_volume():
    docs = list(yaml.safe_load_all((Path(__file__).resolve().parents[1] / "k8s" / "remediation-dispatcher.yaml").read_text(encoding="utf-8")))
    dep = next(d for d in docs if d and d["kind"] == "Deployment")
    pod = dep["spec"]["template"]["spec"]
    assert pod["securityContext"]["fsGroup"] == 10001
    assert {"name": "EXEC_LOG", "value": "/data/executions.jsonl"} in pod["containers"][0]["env"]
    assert pod["volumes"][0]["persistentVolumeClaim"]["claimName"] == "remediation-dispatcher-history"
    assert any(d and d["kind"] == "PersistentVolumeClaim" and d["metadata"]["name"] == "remediation-dispatcher-history" for d in docs)


# ----------------------------------------------------------------------------------------------------- memory: scale out AND replace the pods
def test_the_memory_fix_scales_out_and_replaces_the_pods(isolated):
    plan = disp.decide(MEM)
    assert plan["playbook"] == "scale_replicas.yml" and plan["extra_vars"]["replace_pods"] is True and plan["extra_vars"]["replicas"] == 3
    rec = disp.run_execution(_accept(MEM), sleep=lambda s: None)
    assert rec["status"] == "succeeded" and rec["after"]["generation"] == 3          # one for the scale, one for rolling the pods
    assert yaml_vars(isolated.ran[-1])["replace_pods"] is True


def test_a_scale_out_that_left_the_old_pods_running_is_not_a_recovery(isolated):
    isolated.roll = False                                   # the playbook "succeeded" but the pods were never rolled
    rec = disp.run_execution(_accept(MEM), sleep=lambda s: None)
    assert rec["status"] == "verification_failed" and "pods were not replaced" in rec["reason"]
    assert rec["rolled_back_to"] == 1                       # and the scale-out is undone as before


def test_when_the_count_is_already_right_rolling_the_pods_is_enough(isolated):
    isolated.state.update(replicas=3, updated=3, ready=3, available=3)
    rec = disp.run_execution(_accept(MEM), sleep=lambda s: None)
    assert rec["status"] == "succeeded" and rec["after"]["generation"] == 2          # only the roll changed the generation


def test_the_scale_playbook_replaces_pods_only_when_asked_and_waits_for_the_new_ones():
    text = (ROOT / "ansible" / "playbooks" / "scale_replicas.yml").read_text()
    assert "replace_pods: false" in text and "replace_pods is boolean" in text           # off by default, and guarded
    assert text.count("when: replace_pods | bool") == 3 and "restartedAt" in text
    assert "after_scale.resources[0].metadata.generation" in text                        # waits for a roll AFTER the scale, not the scale itself
    assert (ROOT / "ansible" / "remediation_map.yml").read_text().count("replace_pods: true") == 1       # only the memory cause uses it
