"""Tests for the Alertmanager webhook and the correlation data tables.

The algorithm itself is not implemented yet (build step 4), so these tests
cover the wiring: the webhook accepts a real Alertmanager payload, the
dependency chain is ordered correctly, and every signature points at a
playbook that exists on disk.
"""

from pathlib import Path

from dependency_graph import CHAIN, depth, is_known
from signatures import SIGNATURES, signatures_for

PLAYBOOK_DIR = Path(__file__).resolve().parents[2] / "ansible" / "playbooks"


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_webhook_accepts_alertmanager_payload(client, sample_payload):
    response = client.post("/alerts", json=sample_payload)
    assert response.status_code == 202
    body = response.json()
    assert body["received_alerts"] == 4
    assert body["status"] == "correlated"
    assert body["root_cause"] == "db_connection_pool_exhaustion"
    assert body["service"] == "transaction-service"
    assert body["playbook"] == "restart_service.yml"
    assert body["confidence"] == 1.0


def test_webhook_ignores_resolved_alerts(client, sample_payload):
    sample_payload["alerts"][0]["status"] = "resolved"
    response = client.post("/alerts", json=sample_payload)
    assert response.status_code == 202
    assert response.json()["received_alerts"] == 3


def test_resolved_notifications_are_not_counted_as_incidents_or_escalations(client, sample_payload):
    """Regression, found on a live cluster: the dashboard showed 48 "escalated to a human"
    after a run with a handful of real escalations, because every "resolved" notification
    Alertmanager sent was scored as an incident the engine failed to diagnose."""
    from prometheus_client import REGISTRY

    def value(name):
        return REGISTRY.get_sample_value(name, {"service": "correlation-engine"}) or 0.0

    before = {n: value(n) for n in
              ("incidents_received_total", "unmatched_incidents_total", "resolved_notifications_total")}
    for alert in sample_payload["alerts"]:
        alert["status"] = "resolved"
    sample_payload["status"] = "resolved"

    body = client.post("/alerts", json=sample_payload).json()

    assert body["status"] == "resolved" and body["action"] == "none"
    assert value("incidents_received_total") == before["incidents_received_total"]
    assert value("unmatched_incidents_total") == before["unmatched_incidents_total"]
    assert value("resolved_notifications_total") == before["resolved_notifications_total"] + 1


def test_unmatched_incident_escalates_to_a_human(client, sample_payload):
    # Drop the deciding symptom: what remains is consequences with no cause.
    sample_payload["alerts"] = [
        a for a in sample_payload["alerts"]
        if a["labels"]["alertname"] != "DBPoolNearExhaustion"
    ]
    body = client.post("/alerts", json=sample_payload).json()
    assert body["root_cause"] is None
    assert body["status"] == "unmatched"
    assert body["action"] == "escalate_to_human"
    assert "DBPoolNearExhaustion" in body["reason"]


def test_database_is_the_deepest_service():
    # Root-cause ranking depends on this ordering (brief section 5 step 3).
    assert CHAIN[0] == "database"
    assert CHAIN[-1] == "frontend"
    assert depth("database") < depth("transaction-service") < depth("frontend")


def test_unknown_service_has_no_depth():
    assert depth("payments-service") is None
    assert not is_known("payments-service")


def test_signatures_are_owned_by_a_service_in_the_chain():
    for cause, spec in SIGNATURES.items():
        assert is_known(spec["owner"]), f"{cause} is owned by an unknown service"


def test_required_symptoms_are_a_subset_of_expected():
    for cause, spec in SIGNATURES.items():
        missing = set(spec["required_symptoms"]) - set(spec["expected_symptoms"])
        assert not missing, f"{cause} requires symptoms it does not expect: {missing}"


def test_every_signature_points_at_a_real_playbook():
    for cause, spec in SIGNATURES.items():
        playbook = PLAYBOOK_DIR / spec["playbook"]
        assert playbook.is_file(), f"{cause} references missing {playbook.name}"


def test_signatures_for_filters_by_owner():
    assert "db_disk_full" in signatures_for("database")
    assert "db_disk_full" not in signatures_for("transaction-service")


def test_every_alert_rule_can_reach_the_engine_through_alertmanager():
    """Each rule must carry the labels the Alertmanager route and the engine key on.

    The route forwards only `namespace = "autosre"`, and rules that aggregate with
    `sum by (service)` drop that label - so it has to be set explicitly, or those
    alerts silently go to the null receiver. The engine in turn needs `service`.
    A literal label is checked, not a templated one, since templates are resolved
    per alert and `service` for the restart/memory rules legitimately is one.
    """
    import yaml

    rules_file = Path(__file__).resolve().parents[2] / "monitoring" / "alert-rules.yaml"
    values_file = Path(__file__).resolve().parents[2] / "monitoring" / "kube-prometheus-stack-values.yaml"
    spec = yaml.safe_load(rules_file.read_text(encoding="utf-8"))
    route = yaml.safe_load(values_file.read_text(encoding="utf-8"))["alertmanager"]["config"]["route"]
    matcher = route["routes"][0]["matchers"][0]
    assert matcher == 'namespace = "autosre"', matcher

    from dependency_graph import CHAIN

    for group in spec["spec"]["groups"]:
        for rule in group["rules"]:
            labels = rule.get("labels", {})
            assert labels.get("namespace") == "autosre", f"{rule['alert']} would not be routed"
            service = labels.get("service")
            if service and "{{" not in service:
                assert service in CHAIN, f"{rule['alert']} labels unknown service {service!r}"
            if not service:
                # No explicit service label, so it must survive from the metric. A rule
                # that does not aggregate keeps every label of its series; one that
                # does (`sum by (...)`) keeps only what is named, so `service` must be.
                import re

                groupings = re.findall(r"\bby\s*\(([^)]*)\)", rule["expr"])
                for names in groupings:
                    assert "service" in [n.strip() for n in names.split(",")], (
                        f"{rule['alert']} aggregates by ({names.strip()}) and drops `service`"
                    )


def test_every_expected_symptom_has_a_prometheus_rule():
    """The alert names in signatures.py must exist in monitoring/alert-rules.yaml.

    This is the contract described in the README: a signature that references an
    alert nobody fires can never match, and the failure is silent at runtime.
    """
    import yaml

    rules_file = Path(__file__).resolve().parents[2] / "monitoring" / "alert-rules.yaml"
    spec = yaml.safe_load(rules_file.read_text(encoding="utf-8"))
    defined = {
        rule["alert"]
        for group in spec["spec"]["groups"]
        for rule in group["rules"]
        if "alert" in rule
    }

    referenced = {
        symptom
        for signature in SIGNATURES.values()
        for symptom in signature["expected_symptoms"]
    }

    missing = referenced - defined
    assert not missing, f"signatures reference alerts with no Prometheus rule: {missing}"


def test_capture_records_what_the_engine_decided_not_just_what_it_received(client, sample_payload, tmp_path, monkeypatch):
    import json
    import app as engine
    path = tmp_path / "incidents.jsonl"
    monkeypatch.setattr(engine, "CAPTURE_PATH", str(path))
    monkeypatch.setattr(engine, "SECOND_OPINION_URL", None)
    client.post("/alerts", json=sample_payload)
    record = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert record["response"]["root_cause"] == "db_connection_pool_exhaustion"
    assert record["response"]["service"] == "transaction-service"
    assert record["payload"]["alerts"]                                # the replay tooling still works
    # an unmatched incident is captured too, with its escalation
    sample_payload["alerts"] = [a for a in sample_payload["alerts"]
                                if a["labels"]["alertname"] != "DBPoolNearExhaustion"]
    client.post("/alerts", json=sample_payload)
    last = json.loads(path.read_text(encoding="utf-8").splitlines()[-1])
    assert last["response"]["status"] == "unmatched" and last["response"]["action"] == "escalate_to_human"


def test_engine_escalates_two_simultaneous_faults_only_when_the_rule_is_on(client, monkeypatch):
    import app as engine
    alerts = [{"status": "firing", "labels": {"alertname": n, "service": s}}
              for n, s in (("DBDiskNearFull", "database"), ("ContainerMemoryHigh", "transaction-service"))]
    payload = {"alerts": alerts}
    monkeypatch.setattr(engine, "ESCALATE_ON_AMBIGUITY", False)
    assert client.post("/alerts", json=payload).json()["root_cause"] == "db_disk_full"
    monkeypatch.setattr(engine, "ESCALATE_ON_AMBIGUITY", True)
    body = client.post("/alerts", json=payload).json()
    assert body["status"] == "unmatched" and body["action"] == "escalate_to_human"
    assert "together" in body["reason"]
