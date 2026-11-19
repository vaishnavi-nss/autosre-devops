"""After a fix the effect symptoms clear later than the deciding alert. A notification that holds only those must not be escalated as an unknown cause."""
import app as engine


def alerts(*pairs):
    return {"receiver": "x", "status": "firing", "commonLabels": {}, "groupLabels": {},
            "alerts": [{"status": "firing", "labels": {"alertname": n, "service": s, "severity": "warning"}, "annotations": {}, "startsAt": "2026-11-19T10:43:00Z"} for n, s in pairs]}


def test_effect_symptoms_after_a_diagnosed_fault_are_residual_not_an_escalation(client):
    first = client.post("/alerts", json=alerts(("DeploymentUnavailable", "database"), ("APILatencyHigh", "api-gateway"))).json()
    assert first["root_cause"] == "database_down"
    tail = client.post("/alerts", json=alerts(("APILatencyHigh", "api-gateway"))).json()
    assert tail["status"] == "residual" and tail["action"] == "none" and tail["root_cause"] is None
    assert "still clearing" in tail["reason"] and tail["residual_of"]
    recorded = client.get("/incidents?limit=5").json()["incidents"][0]
    assert recorded["status"] == "residual" and recorded["residual_of"]


def test_the_same_effects_with_no_recent_diagnosis_are_still_an_unknown_cause_for_a_person(client):
    engine.RECENT_INCIDENTS.clear()
    body = client.post("/alerts", json=alerts(("APILatencyHigh", "api-gateway"))).json()
    assert body["status"] == "unmatched" and body["action"] == "escalate_to_human"


def test_a_non_effect_alert_is_never_called_residual(client):
    client.post("/alerts", json=alerts(("DeploymentUnavailable", "database")))
    body = client.post("/alerts", json=alerts(("PodRestartsHigh", "transaction-service"))).json()
    assert body["status"] != "residual"
