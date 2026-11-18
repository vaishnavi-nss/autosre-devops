"""The causes added in the completion session: a service that is gone, and a rollout that went wrong.

They must be told apart from the three older causes (no overlap in deciding symptoms), keep the project's rule that a lone deciding symptom
does not clear the 0.75 gate, and every alert, playbook and map entry they rely on must exist."""
import re
from pathlib import Path

import pytest

from correlate import ambiguous_causes, correlate
from signatures import SIGNATURES

ROOT = Path(__file__).resolve().parents[2]
NEW = {"database_down": "restore_service.yml", "transaction_service_down": "restore_service.yml", "api_gateway_down": "restore_service.yml",
       "bad_rollout_transaction_service": "rollback_deployment.yml", "bad_rollout_api_gateway": "rollback_deployment.yml",
       "database_unreachable": "fix_service_selector.yml", "transaction_service_unreachable": "fix_service_selector.yml", "api_gateway_unreachable": "fix_service_selector.yml"}


def fire(*pairs):
    return [{"status": "firing", "labels": {"alertname": n, "service": s, "severity": "critical"}} for n, s in pairs]


def test_the_new_causes_exist_with_their_playbooks_and_nothing_else_changed():
    assert {c: SIGNATURES[c]["playbook"] for c in NEW} == NEW
    assert {"db_connection_pool_exhaustion", "high_memory_usage", "db_disk_full"} <= set(SIGNATURES) and len(SIGNATURES) == 11


def test_every_alert_playbook_and_map_entry_the_new_causes_need_exists():
    rules = (ROOT / "monitoring" / "alert-rules.yaml").read_text(encoding="utf-8")
    alerts = set(re.findall(r"-\s+alert:\s*(\w+)", rules))
    for cause in NEW:
        assert set(SIGNATURES[cause]["weights"]) <= alerts, cause
        assert (ROOT / "ansible" / "playbooks" / NEW[cause]).is_file()
    import yaml
    mapping = yaml.safe_load((ROOT / "ansible" / "remediation_map.yml").read_text(encoding="utf-8"))["remediations"]
    assert set(SIGNATURES) == set(mapping)


@pytest.mark.parametrize("service,cause", [("database", "database_down"), ("transaction-service", "transaction_service_down"), ("api-gateway", "api_gateway_down")])
def test_a_service_with_no_replica_is_named_with_its_consequences_above_it(service, cause):
    out = correlate(fire(("DeploymentUnavailable", service), ("ErrorRateHigh", "api-gateway"), ("APILatencyHigh", "api-gateway")))
    assert out["root_cause"] == cause and out["service"] == service and out["playbook"] == "restore_service.yml"
    assert out["confidence"] >= 0.75


def test_the_deepest_service_is_blamed_when_the_database_is_down_and_everything_above_it_fails():
    out = correlate(fire(("DeploymentUnavailable", "database"), ("ErrorRateHigh", "transaction-service"), ("ErrorRateHigh", "api-gateway"), ("APILatencyHigh", "api-gateway")))
    assert out["root_cause"] == "database_down"
    assert ambiguous_causes(fire(("DeploymentUnavailable", "database"), ("ErrorRateHigh", "transaction-service"))) == ["database_down"]


def test_a_crash_looping_revision_reaches_full_confidence_and_a_lone_stall_does_not():
    crash = correlate(fire(("RolloutStalled", "transaction-service"), ("PodRestartsHigh", "transaction-service")))
    assert crash["root_cause"] == "bad_rollout_transaction_service" and crash["confidence"] == 1.0 and crash["playbook"] == "rollback_deployment.yml"
    lone = correlate(fire(("RolloutStalled", "api-gateway")))
    assert lone["root_cause"] == "bad_rollout_api_gateway" and lone["confidence"] < 0.75          # an image-pull or probe failure is left to a person


def test_the_deciding_symptoms_do_not_overlap_with_the_older_causes():
    # the older faults never raise the new deciding symptoms, and the new ones never raise the old ones' deciding symptoms
    pool = fire(("DBPoolNearExhaustion", "transaction-service"), ("ErrorRateHigh", "transaction-service"), ("APILatencyHigh", "api-gateway"), ("PodRestartsHigh", "transaction-service"))
    assert ambiguous_causes(pool) == ["db_connection_pool_exhaustion"]
    mem = fire(("ContainerMemoryHigh", "transaction-service"), ("APILatencyHigh", "api-gateway"), ("PodRestartsHigh", "transaction-service"))
    assert ambiguous_causes(mem) == ["high_memory_usage"]
    disk = fire(("DBDiskNearFull", "database"), ("ErrorRateHigh", "transaction-service"))
    assert ambiguous_causes(disk) == ["db_disk_full"]


def test_two_new_faults_at_once_are_ambiguous():
    both = fire(("DeploymentUnavailable", "database"), ("RolloutStalled", "api-gateway"))
    assert set(ambiguous_causes(both)) == {"database_down", "bad_rollout_api_gateway"}


def test_alert_rules_hold_times_and_service_labels_are_set_for_the_new_alerts():
    rules = (ROOT / "monitoring" / "alert-rules.yaml").read_text(encoding="utf-8")
    for name in ("DeploymentUnavailable", "RolloutStalled"):
        block = rules.split(f"alert: {name}")[1].split("- alert:")[0]
        assert 'service: "{{ $labels.deployment }}"' in block and "namespace: autosre" in block and re.search(r"for:\s*\d+[sm]", block)


@pytest.mark.parametrize("service,cause", [("database", "database_unreachable"), ("transaction-service", "transaction_service_unreachable"), ("api-gateway", "api_gateway_unreachable")])
def test_a_service_with_pods_but_no_endpoint_is_a_selector_problem_not_a_service_that_is_down(service, cause):
    out = correlate(fire(("ServiceHasNoEndpoints", service), ("ErrorRateHigh", "api-gateway"), ("APILatencyHigh", "api-gateway")))
    assert out["root_cause"] == cause and out["playbook"] == "fix_service_selector.yml" and out["confidence"] >= 0.75
    assert ambiguous_causes(fire(("ServiceHasNoEndpoints", service), ("ErrorRateHigh", "api-gateway"))) == [cause]      # never also the 'down' cause


def test_the_alert_that_means_no_endpoint_leaves_out_a_deployment_with_no_replica():
    rules = (ROOT / "monitoring" / "alert-rules.yaml").read_text(encoding="utf-8")
    block = rules.split("alert: ServiceHasNoEndpoints")[1]
    assert "kube_endpointslice_info" in block and "unless on(endpointslice)" in block and "kube_deployment_status_replicas_available" in block and "> 0" in block
