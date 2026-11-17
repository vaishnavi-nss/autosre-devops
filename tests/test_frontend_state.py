"""The dashboard's /api/state: real incident, remediation, rollout and Prometheus data, each part independent of the others."""
import pytest
from fastapi.testclient import TestClient

from services.frontend import app as fe

client = TestClient(fe.app)

ENGINE = {"incidents": [{"received_at": "2026-11-20T10:00:00+00:00", "status": "correlated", "action": None,
                         "root_cause": "db_connection_pool_exhaustion", "service": "transaction-service", "confidence": 1.0,
                         "playbook": "restart_service.yml", "matched_symptoms": ["DBPoolNearExhaustion"],
                         "second_opinion": {"verdict": "agree", "scorer_fault": "db_connection_pool_exhaustion",
                                            "scorer_service": "transaction-service"},
                         "remediation": {"status": "accepted", "execution_id": "abc"}}]}
EXECS = {"executions": [{"id": "abc", "playbook": "restart_service.yml", "status": "succeeded", "duration_s": 41.2, "steps": [{"step": "preflight"}]}]}
DEPLOY = {"namespace": "autosre", "deployments": [{"name": "transaction-service", "replicas": 1, "ready": 1, "updated": 1, "rolling_out": False}]}
HEALTH = {"status": "healthy", "auto_remediate": False, "min_confidence": 0.75, "second_opinion_policy": "agree"}
STATE = {"in_flight": [], "cooldowns": [{"root_cause": "db_connection_pool_exhaustion", "target_service": "transaction-service", "remaining_s": 500}]}


def prom(value):
    return {"data": {"result": [{"value": [0, str(value)]}]}}


ALERTS = {"data": {"result": [{"metric": {"alertname": "DBPoolNearExhaustion", "service": "transaction-service", "severity": "critical"}}]}}


class Reply:
    def __init__(self, data, status=200):
        self.data, self.status = data, status

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")

    def json(self):
        return self.data


def fake_client(routes):
    class Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params=None):
            for key, reply in routes.items():
                if url.endswith(key) and (params is None or "query" not in params or key != "/api/v1/query" or True):
                    if key == "/api/v1/query":
                        q = params["query"]
                        reply = next(v for k, v in routes["prom"].items() if k in q)
                    if isinstance(reply, Exception):
                        raise reply
                    return reply
            raise RuntimeError("no route " + url)
    return Client


def routes(**over):
    base = {"/incidents": Reply(ENGINE), "/executions": Reply(EXECS), "/deployments": Reply(DEPLOY), "/health": Reply(HEALTH),
            "/state": Reply(STATE), "/api/v1/query": None,
            "prom": {"ALERTS": Reply(ALERTS), "db_connection_pool_active": Reply(prom(19)), "db_connection_pool_max": Reply(prom(20)),
                     "db_disk_usage_ratio": Reply(prom(0.42))}}
    base.update(over)
    return base


def test_state_aggregates_every_source(monkeypatch):
    monkeypatch.setattr(fe.httpx, "AsyncClient", fake_client(routes()))
    body = client.get("/api/state").json()
    assert body["incidents"][0]["root_cause"] == "db_connection_pool_exhaustion"
    assert body["incidents"][0]["second_opinion"]["verdict"] == "agree"
    assert body["executions"][0]["status"] == "succeeded" and body["deployments"][0]["name"] == "transaction-service"
    assert body["dispatcher"]["health"]["auto_remediate"] is False and body["dispatcher"]["cooldowns"][0]["remaining_s"] == 500
    assert body["alerts"] == [{"alertname": "DBPoolNearExhaustion", "service": "transaction-service", "severity": "critical"}]
    assert body["pool"] == {"active": 19.0, "max": 20.0} and body["disk"] == pytest.approx(0.42) and body["errors"] == {}


def test_one_dead_dependency_does_not_break_the_rest(monkeypatch):
    monkeypatch.setattr(fe.httpx, "AsyncClient", fake_client(routes(**{"/incidents": ConnectionError("engine down")})))
    body = client.get("/api/state").json()
    assert body["incidents"] == [] and "engine" in body["errors"] and "engine down" in body["errors"]["engine"]
    assert body["executions"] and body["alerts"] and body["pool"]["active"] == 19.0


def test_everything_down_still_answers(monkeypatch):
    dead = {k: ConnectionError("down") for k in ("/incidents", "/executions", "/deployments", "/health", "/state")}
    r = routes(**dead)
    r["prom"] = {k: ConnectionError("down") for k in r["prom"]}
    monkeypatch.setattr(fe.httpx, "AsyncClient", fake_client(r))
    body = client.get("/api/state").json()
    assert body["incidents"] == [] and body["executions"] == [] and body["alerts"] == []
    assert body["pool"] == {"active": None, "max": None} and body["disk"] is None and set(body["errors"]) == {"engine", "dispatcher", "prometheus"}


def test_the_page_shows_real_panels_and_no_placeholders():
    html = client.get("/").text
    for element in ("inc-cause", "inc-conf", "inc-ml", "inc-play", "inc-disp", "exec-rows", "rollout-rows", "alert-list", "recovery-badge"):
        assert f'id="{element}"' in html, element
    assert "/api/state" in html and "function esc(" in html            # values from services are escaped before they reach innerHTML
    for stale in ("awaiting_telemetry", "ENGAGED (10m)", "2 / 20", "&ge; 18 active"):
        assert stale not in html, stale


def test_meta_alerts_are_not_shown_as_firing_incidents():
    """Seen in the rendered page on the real cluster: InfoInhibitor (a meta alert) was listed as an active alert with a red badge."""
    import inspect
    assert 'alertname!~"InfoInhibitor|Watchdog"' in inspect.getsource(fe.state)


def test_page_shows_the_acted_on_incident_not_the_follow_up_noise_after_a_fix():
    """Seen in the rendered page on the real cluster: after a successful restart the engine raised an 'unmatched' incident while alerts were
    still clearing, and the panel said 'Escalated to a human' above a 'Fix verified' panel."""
    html = client.get("/").text
    assert "function pickIncident(st)" in html and "const inc = pickIncident(st);" in html
    assert "<= 300000" in html
