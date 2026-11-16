"""GET /incidents: the dashboard's view of what the engine decided. Read-only; it never influences a decision."""
import json
from pathlib import Path


def _payload(*pairs):
    return {"alerts": [{"status": "firing", "labels": {"alertname": n, "service": s}} for n, s in pairs]}


def test_a_correlated_incident_is_listed_with_cause_confidence_verdict_and_dispatch(client, monkeypatch):
    import app as engine
    engine.RECENT_INCIDENTS.clear()
    monkeypatch.setattr(engine, "SECOND_OPINION_URL", None)
    monkeypatch.setattr(engine, "DISPATCHER_ENABLED", False)
    body = client.post("/alerts", json=_payload(("DBPoolNearExhaustion", "transaction-service"), ("ErrorRateHigh", "transaction-service"))).json()
    assert body["status"] == "correlated"
    item = client.get("/incidents").json()["incidents"][0]
    assert item["root_cause"] == "db_connection_pool_exhaustion" and item["service"] == "transaction-service"
    assert item["confidence"] == body["confidence"] and item["playbook"] == "restart_service.yml"
    assert {a["alertname"] for a in item["alerts"]} == {"DBPoolNearExhaustion", "ErrorRateHigh"}
    assert item["matched_symptoms"] == ["DBPoolNearExhaustion", "ErrorRateHigh"] and item["remediation"]["status"] == "disabled"


def test_an_unmatched_incident_is_listed_as_an_escalation(client, monkeypatch):
    import app as engine
    engine.RECENT_INCIDENTS.clear()
    monkeypatch.setattr(engine, "SECOND_OPINION_URL", None)
    client.post("/alerts", json=_payload(("ErrorRateHigh", "transaction-service"), ("APILatencyHigh", "api-gateway")))
    item = client.get("/incidents").json()["incidents"][0]
    assert item["status"] == "unmatched" and item["action"] == "escalate_to_human" and item["root_cause"] is None and item["reason"]


def test_resolved_notifications_are_not_incidents_and_the_list_is_newest_first_and_bounded(client, monkeypatch):
    import app as engine
    engine.RECENT_INCIDENTS.clear()
    monkeypatch.setattr(engine, "SECOND_OPINION_URL", None)
    monkeypatch.setattr(engine, "DISPATCHER_ENABLED", False)
    client.post("/alerts", json={"alerts": [{"status": "resolved", "labels": {"alertname": "ErrorRateHigh", "service": "transaction-service"}}]})
    assert client.get("/incidents").json()["incidents"] == []
    client.post("/alerts", json=_payload(("DBDiskNearFull", "database")))
    client.post("/alerts", json=_payload(("ContainerMemoryHigh", "transaction-service")))
    assert [i["root_cause"] for i in client.get("/incidents").json()["incidents"]] == ["high_memory_usage", "db_disk_full"]
    for _ in range(60):
        client.post("/alerts", json=_payload(("DBDiskNearFull", "database")))
    assert len(client.get("/incidents?limit=500").json()["incidents"]) == 50
