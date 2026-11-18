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
    from collections import deque
    engine.RECENT_INCIDENTS = deque(engine.RECENT_INCIDENTS, maxlen=50)          # the bound is the deque's, so the test shrinks it
    for _ in range(60):
        client.post("/alerts", json=_payload(("DBDiskNearFull", "database")))
    assert len(client.get("/incidents?limit=500").json()["incidents"]) == 50
    engine.RECENT_INCIDENTS = deque(maxlen=engine.HISTORY_KEEP)


# ---------------------------------------------------------------------------------------------------- persistence and events
def test_incidents_get_stable_ids_and_survive_a_restart(client, monkeypatch, tmp_path):
    import app as engine
    from collections import deque
    log_file = tmp_path / "incidents.jsonl"
    monkeypatch.setattr(engine, "INCIDENT_LOG", str(log_file))
    monkeypatch.setattr(engine, "SECOND_OPINION_URL", None)
    monkeypatch.setattr(engine, "DISPATCHER_ENABLED", False)
    engine.RECENT_INCIDENTS.clear(); engine._seq["incident"] = 0
    client.post("/alerts", json=_payload(("DBDiskNearFull", "database")))
    client.post("/alerts", json=_payload(("ContainerMemoryHigh", "transaction-service")))
    first = client.get("/incidents").json()["incidents"]
    assert [i["id"] for i in first] == ["INC-0002", "INC-0001"]
    assert len(log_file.read_text(encoding="utf-8").splitlines()) == 2
    # a restart: empty memory, same file
    monkeypatch.setattr(engine, "RECENT_INCIDENTS", deque(maxlen=500)); engine._seq["incident"] = 0
    engine._restore_history()
    again = client.get("/incidents").json()["incidents"]
    assert [i["id"] for i in again] == ["INC-0002", "INC-0001"] and again[0]["root_cause"] == "high_memory_usage"
    client.post("/alerts", json=_payload(("DBDiskNearFull", "database")))
    assert client.get("/incidents").json()["incidents"][0]["id"] == "INC-0003"        # numbering continues, never reused
    engine.RECENT_INCIDENTS.clear(); engine._seq["incident"] = 0


def test_a_full_or_missing_volume_never_stops_the_engine(client, monkeypatch, tmp_path):
    import app as engine
    monkeypatch.setattr(engine, "INCIDENT_LOG", str(tmp_path / "no-such-dir" / "incidents.jsonl"))
    monkeypatch.setattr(engine, "SECOND_OPINION_URL", None)
    monkeypatch.setattr(engine, "DISPATCHER_ENABLED", False)
    assert client.post("/alerts", json=_payload(("DBDiskNearFull", "database"))).status_code == 202


def test_events_are_recorded_filtered_and_whitelisted(client, monkeypatch, tmp_path):
    import app as engine
    monkeypatch.setattr(engine, "EVENT_LOG", str(tmp_path / "events.jsonl"))
    engine.RECENT_EVENTS.clear(); engine._seq["event"] = 0
    assert client.post("/events", json={"kind": "nonsense"}).status_code == 422
    ok = client.post("/events", json={"kind": "change", "setting": "live_remediation", "value": "on", "by": "fault console", "evil": "x", "nested": {"a": 1}})
    assert ok.status_code == 201 and ok.json()["id"] == "EVT-0001" and "evil" not in ok.json() and "nested" not in ok.json() and ok.json()["t"]
    client.post("/events", json={"kind": "run", "name": "pool", "mode": "live", "result": "passed"})
    assert [e["kind"] for e in client.get("/events").json()["events"]] == ["run", "change"]
    assert [e["name"] for e in client.get("/events?kind=run").json()["events"]] == ["pool"]
    assert client.post("/events", json={"kind": "change", "text": "x" * 5000}).status_code == 413
    assert len((tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()) == 2
    engine.RECENT_EVENTS.clear(); engine._seq["event"] = 0
