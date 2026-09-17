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
    # Until build step 4 lands, every incident is reported as uncorrelated.
    assert body["root_cause"] is None
    assert body["status"] == "correlation not implemented"


def test_webhook_ignores_resolved_alerts(client, sample_payload):
    sample_payload["alerts"][0]["status"] = "resolved"
    response = client.post("/alerts", json=sample_payload)
    assert response.status_code == 202
    assert response.json()["received_alerts"] == 3


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
