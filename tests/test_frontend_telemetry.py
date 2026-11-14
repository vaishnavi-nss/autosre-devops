"""Offline API and browser-script checks; no services, playbooks, or cluster needed."""
import importlib.util
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("frontend_telemetry_app", ROOT / "services/frontend/app.py")
frontend = importlib.util.module_from_spec(spec)
spec.loader.exec_module(frontend)
ASYNC_CLIENT = httpx.AsyncClient


@pytest.fixture
def telemetry():
    return {"status": "ok", "remediation": {
        "id": "recorded-job", "root_cause": "db_connection_pool_exhaustion", "service": "transaction-service",
        "requested_at": 1000.0, "status": "succeeded", "confidence": None, "playbook": "restart_service.yml",
        "details": {"previous_generation": 8, "generation": 9, "previous_instance": "old-pod",
                    "recovery": {"instance": "recovered-pod", "pool_utilization": 0.1}}},
        "settings": {"auto_remediate": True, "min_confidence": 0.75, "cooldown_seconds": 600},
        "cooldown": {"active": False, "remaining_seconds": 0, "operator_review_required": False}}


def upstream(monkeypatch, handler):
    seen = []
    def handle(request):
        seen.append(request)
        return handler(request)
    def client(**kwargs):
        assert kwargs["timeout"] == 2.0
        return ASYNC_CLIENT(transport=httpx.MockTransport(handle), **kwargs)
    monkeypatch.setattr(frontend.httpx, "AsyncClient", client)
    return seen


def test_proxy_preserves_completed_history_without_fabricating_confidence(monkeypatch, telemetry):
    seen = upstream(monkeypatch, lambda request: httpx.Response(200, json=telemetry))
    response = TestClient(frontend.app).get("/api/remediation")
    assert response.status_code == 200 and response.json() == telemetry
    assert response.headers["cache-control"] == "no-store"
    assert [(r.method, r.url.path) for r in seen] == [("GET", "/remediations/latest")]


def test_proxy_preserves_actual_new_job_confidence(monkeypatch, telemetry):
    telemetry["remediation"]["confidence"] = 0.86
    upstream(monkeypatch, lambda request: httpx.Response(200, json=telemetry))
    assert TestClient(frontend.app).get("/api/remediation").json()["remediation"]["confidence"] == 0.86


def test_proxy_empty_is_distinct_from_unavailable(monkeypatch, telemetry):
    telemetry.update(status="empty", remediation=None)
    upstream(monkeypatch, lambda request: httpx.Response(200, json=telemetry))
    response = TestClient(frontend.app).get("/api/remediation")
    assert response.status_code == 200 and response.json()["status"] == "empty"


@pytest.mark.parametrize("status", [404, 500, 503])
def test_proxy_backend_http_failure_is_graceful(monkeypatch, status):
    upstream(monkeypatch, lambda request: httpx.Response(status, text="private backend error"))
    response = TestClient(frontend.app).get("/api/remediation")
    assert response.status_code == 503 and response.json()["status"] == "unavailable"
    assert "private backend error" not in response.text


@pytest.mark.parametrize("error", [httpx.ReadTimeout, httpx.ConnectError])
def test_proxy_transport_failure_is_graceful(monkeypatch, error):
    def fail(request):
        raise error("backend unavailable", request=request)
    upstream(monkeypatch, fail)
    response = TestClient(frontend.app).get("/api/remediation")
    assert response.status_code == 503 and response.json()["remediation"] is None


@pytest.mark.parametrize("mutation", ["inconsistent", "confidence", "timestamp", "details", "settings", "status"])
def test_proxy_malformed_contract_is_unavailable(monkeypatch, telemetry, mutation):
    if mutation == "inconsistent":
        telemetry["remediation"] = None
    elif mutation == "confidence":
        telemetry["remediation"]["confidence"] = 1.5
    elif mutation == "timestamp":
        telemetry["remediation"]["requested_at"] = -1
    elif mutation == "details":
        telemetry["remediation"]["details"] = []
    elif mutation == "settings":
        telemetry["settings"]["auto_remediate"] = "false"
    else:
        telemetry["remediation"]["status"] = "invented"
    upstream(monkeypatch, lambda request: httpx.Response(200, json=telemetry))
    assert TestClient(frontend.app).get("/api/remediation").status_code == 503


def test_proxy_non_json_is_unavailable(monkeypatch):
    upstream(monkeypatch, lambda request: httpx.Response(200, text="<html>not JSON</html>"))
    assert TestClient(frontend.app).get("/api/remediation").status_code == 503


def test_configurable_dispatcher_url(monkeypatch, telemetry):
    monkeypatch.setattr(frontend, "REMEDIATION_URL", "http://dispatcher.example:8001/")
    seen = upstream(monkeypatch, lambda request: httpx.Response(200, json=telemetry))
    TestClient(frontend.app).get("/api/remediation")
    assert str(seen[0].url) == "http://dispatcher.example:8001/remediations/latest"


def test_invalid_dispatcher_configuration_is_unavailable(monkeypatch):
    monkeypatch.setattr(frontend, "REMEDIATION_URL", "http://[")
    response = TestClient(frontend.app).get("/api/remediation")
    assert response.status_code == 503 and response.json()["status"] == "unavailable"


@pytest.mark.parametrize("status", [200, 503])
def test_transaction_poll_contract_is_unchanged(monkeypatch, status):
    seen = upstream(monkeypatch, lambda request: httpx.Response(status))
    assert TestClient(frontend.app).get("/api/poll").json() == {"status": status}
    assert [(r.method, r.url.path) for r in seen] == [("GET", "/api/v1/trade")]


def test_transaction_connection_failure_is_unchanged(monkeypatch):
    def fail(request):
        raise httpx.ConnectError("gateway unavailable", request=request)
    upstream(monkeypatch, fail)
    assert TestClient(frontend.app).get("/api/poll").json() == {"status": 503}


def test_full_offline_read_path_from_persisted_job(monkeypatch, tmp_path):
    directory = ROOT / "remediation-dispatcher"
    sys.path.insert(0, str(directory))
    backend_spec = importlib.util.spec_from_file_location("frontend_test_dispatcher", directory / "app.py")
    backend = importlib.util.module_from_spec(backend_spec)
    backend_spec.loader.exec_module(backend)
    from state import RemediationState
    backend.STATE = RemediationState(tmp_path / "history.sqlite3")
    _, job = backend.STATE.reserve(backend.ROOT_CAUSE, backend.TARGET,
                                  confidence=0.89, playbook="restart_service.yml")
    backend.STATE.finish(job["id"], "succeeded", {"generation": 9, "recovery": {"instance": "real-record"}})
    original = backend.STATE.path.read_bytes()
    with TestClient(backend.app) as dispatcher:
        upstream(monkeypatch, lambda request: httpx.Response(200, json=dispatcher.get(request.url.path).json()))
        response = TestClient(frontend.app).get("/api/remediation")
    recorded = response.json()["remediation"]
    assert recorded["id"] == job["id"] and recorded["confidence"] == 0.89
    assert recorded["details"]["recovery"]["instance"] == "real-record"
    assert backend.STATE.path.read_bytes() == original


NODE_HARNESS = r"""
const vm = require('node:vm');
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const elements = {};
function node(id) {
  if (!elements[id]) elements[id] = {textContent:'', innerText:'', className:'', style:{},
    classList:{add(){},remove(){}}, children:[], insertAdjacentHTML(){}};
  return elements[id];
}
const intervals = [], timers = [];
const sandbox = {document:{getElementById:node}, AbortController,
  performance:{now:()=>0}, fetch:()=>new Promise(()=>{}),
  setInterval:(fn, ms)=>intervals.push(ms),
  setTimeout:(fn, ms)=>{timers.push(ms);return timers.length}, clearTimeout:()=>{}};
vm.createContext(sandbox);
vm.runInContext(input.script, sandbox);
node('status-badge').innerText = 'Operational';
node('dash-badge').innerText = 'Operational';
sandbox.renderRemediation(input.data);
console.log(JSON.stringify({elements, intervals}));
"""


def render(data):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is needed for browser-script rendering checks")
    html = TestClient(frontend.app).get("/").text
    script = re.search(r"<script>(.*?)</script>", html, re.S).group(1)
    result = subprocess.run([node, "-e", NODE_HARNESS], input=json.dumps({"script": script, "data": data}),
                            capture_output=True, text=True, check=True)
    return json.loads(result.stdout), html


def test_browser_shows_real_history_and_keeps_current_health_separate(telemetry):
    output, html = render(telemetry)
    nodes = output["elements"]
    diagnosis = json.loads(nodes["correlation-payload"]["textContent"])
    assert diagnosis == {"root_cause": "db_connection_pool_exhaustion", "service": "transaction-service",
                         "confidence": None, "playbook": "restart_service.yml"}
    assert nodes["confidence-note"]["textContent"] == "Rule confidence: Not recorded"
    assert nodes["remediation-job-status"]["textContent"] == "succeeded"
    assert "recovered-pod" in nodes["recovery-evidence"]["textContent"]
    assert nodes["status-badge"]["innerText"] == "Operational"
    assert nodes["dash-badge"]["innerText"] == "Operational"
    assert output["intervals"] == [1500]
    assert "historical, not current service health" in html
    assert "awaiting_telemetry" not in html and "Self-healing triggered" not in html


def test_browser_renders_confidence_zero_as_recorded_not_missing(telemetry):
    telemetry["remediation"]["confidence"] = 0.0
    output, _ = render(telemetry)
    assert output["elements"]["confidence-note"]["textContent"] == "Rule confidence: 0"


@pytest.mark.parametrize("state,message", [("empty", "No remediation recorded"), ("unavailable", "Telemetry unavailable")])
def test_browser_missing_and_unavailable_are_distinct(telemetry, state, message):
    telemetry.update(status=state, remediation=None)
    output, _ = render(telemetry)
    assert output["elements"]["correlation-payload"]["textContent"] == message


def test_browser_failed_history_does_not_mark_current_service_unhealthy(telemetry):
    telemetry["remediation"].update(status="failed", details={"reason": "Rollout failed"})
    telemetry["cooldown"].update(operator_review_required=True)
    output, _ = render(telemetry)
    nodes = output["elements"]
    assert nodes["remediation-job-status"]["textContent"] == "failed"
    assert nodes["remediation-cooldown"]["textContent"] == "Operator review required"
    assert nodes["status-badge"]["innerText"] == "Operational"


def test_browser_treats_backend_strings_as_text(telemetry):
    telemetry["remediation"]["root_cause"] = '<img src=x onerror="alert(1)">'
    output, _ = render(telemetry)
    assert '<img src=x' in output["elements"]["correlation-payload"]["textContent"]
    assert "innerHTML" not in output["elements"]["correlation-payload"]
