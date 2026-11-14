"""Tests for the correlation algorithm (brief section 5, steps 2-5).

Incidents are built from (alertname, service) pairs so each scenario reads as
the symptoms an operator would actually see.
"""

import pytest

from correlate import (
    alert_names,
    confidence,
    correlate,
    deepest_alerting_service,
    match_signature,
    services_by_depth,
    why_unmatched,
)
from signatures import SIGNATURES


def incident(*pairs: tuple[str, str], status: str = "firing") -> list[dict]:
    return [
        {"status": status, "labels": {"alertname": name, "service": service}}
        for name, service in pairs
    ]


# -- the three causes in brief section 6 ---------------------------------------

POOL = [
    ("APILatencyHigh", "api-gateway"),
    ("ErrorRateHigh", "transaction-service"),
    ("PodRestartsHigh", "transaction-service"),
    ("DBPoolNearExhaustion", "transaction-service"),
]
DISK = [
    ("DBDiskNearFull", "database"),
    ("ErrorRateHigh", "transaction-service"),
    ("APILatencyHigh", "api-gateway"),
]
MEMORY = [
    ("ContainerMemoryHigh", "transaction-service"),
    ("PodRestartsHigh", "transaction-service"),
    ("APILatencyHigh", "api-gateway"),
]


@pytest.mark.parametrize(
    "symptoms, cause, service, playbook",
    [
        (POOL, "db_connection_pool_exhaustion", "transaction-service", "restart_service.yml"),
        (DISK, "db_disk_full", "database", "clear_logs.yml"),
        (MEMORY, "high_memory_usage", "transaction-service", "scale_replicas.yml"),
    ],
)
def test_full_incident_identifies_its_cause(symptoms, cause, service, playbook):
    result = correlate(incident(*symptoms))
    assert result["root_cause"] == cause
    assert result["service"] == service
    assert result["playbook"] == playbook
    assert result["confidence"] == 1.0
    assert result["missing_symptoms"] == []


def test_result_carries_the_contract_keys_the_dispatcher_reads():
    result = correlate(incident(*POOL))
    assert {"root_cause", "service", "confidence", "playbook"} <= set(result)


# -- step 3: root cause ranking -------------------------------------------------

def test_deepest_service_is_chosen_not_the_loudest():
    alerts = incident(
        ("ErrorRateHigh", "frontend"),
        ("APILatencyHigh", "api-gateway"),
        ("DBPoolNearExhaustion", "transaction-service"),
    )
    assert deepest_alerting_service(alerts) == "transaction-service"


def test_services_are_ordered_deepest_first():
    alerts = incident(
        ("A", "frontend"), ("B", "database"), ("C", "api-gateway"), ("D", "transaction-service")
    )
    assert services_by_depth(alerts) == [
        "database", "transaction-service", "api-gateway", "frontend",
    ]


def test_services_outside_the_chain_are_ignored():
    alerts = incident(("PodRestartsHigh", "kube-system"), ("ErrorRateHigh", "frontend"))
    assert deepest_alerting_service(alerts) == "frontend"


def test_no_alerts_means_no_service():
    assert deepest_alerting_service([]) is None


# -- step 5: the weighted confidence model --------------------------------------

def test_missing_a_consequence_lowers_confidence_but_still_identifies():
    # Pool exhaustion without the latency symptom: 1.0 - 0.10.
    alerts = incident(
        ("DBPoolNearExhaustion", "transaction-service"),
        ("ErrorRateHigh", "transaction-service"),
        ("PodRestartsHigh", "transaction-service"),
    )
    result = correlate(alerts)
    assert result["root_cause"] == "db_connection_pool_exhaustion"
    assert result["confidence"] == 0.9
    assert result["missing_symptoms"] == ["APILatencyHigh"]


def test_confidence_is_not_limited_to_round_quarters():
    # The plain matched/expected ratio over four symptoms can only give
    # 0.25/0.5/0.75/1.0. The weighted model can reach anything in between.
    seen = {
        confidence("db_connection_pool_exhaustion", incident(*subset))
        for subset in [
            [("DBPoolNearExhaustion", "transaction-service")],
            [("DBPoolNearExhaustion", "transaction-service"), ("APILatencyHigh", "api-gateway")],
            [("DBPoolNearExhaustion", "transaction-service"), ("ErrorRateHigh", "transaction-service")],
        ]
    }
    assert seen == {0.55, 0.65, 0.75}


def test_the_deciding_symptom_alone_is_below_the_dispatch_threshold():
    # Default dispatcher gate is 0.75; one lone deciding symptom must not clear it.
    for cause, spec in SIGNATURES.items():
        lone = incident((spec["required_symptoms"][0], spec["owner"]))
        assert confidence(cause, lone) < 0.75, cause


def test_weights_sum_to_one():
    for cause, spec in SIGNATURES.items():
        assert sum(spec["weights"].values()) == pytest.approx(1.0), cause


def test_deciding_symptom_outweighs_every_consequence():
    for cause, spec in SIGNATURES.items():
        deciding = max(spec["weights"][s] for s in spec["required_symptoms"])
        others = [w for s, w in spec["weights"].items() if s not in spec["required_symptoms"]]
        assert deciding > max(others), cause


# -- failing safe: None means escalate to a human --------------------------------

def test_consequences_without_a_deciding_symptom_escalate():
    alerts = incident(
        ("APILatencyHigh", "api-gateway"),
        ("ErrorRateHigh", "transaction-service"),
        ("PodRestartsHigh", "transaction-service"),
    )
    assert correlate(alerts) is None


def test_frontend_only_alerts_escalate():
    assert correlate(incident(("ErrorRateHigh", "frontend"))) is None


def test_deciding_symptom_on_the_wrong_service_is_not_evidence():
    # DBPoolNearExhaustion is exported by transaction-service. The same alert
    # name on the gateway is a coincidence and must not identify the cause.
    assert correlate(incident(("DBPoolNearExhaustion", "api-gateway"))) is None


def test_owner_check_is_what_rejects_a_misplaced_deciding_symptom():
    # transaction-service is the deepest alerting service and owns the pool
    # signature, but the deciding symptom fired on the gateway, not on it. This
    # reaches the owner check (the case above returns early: the gateway owns
    # no signatures at all).
    alerts = incident(
        ("ErrorRateHigh", "transaction-service"),
        ("DBPoolNearExhaustion", "api-gateway"),
    )
    assert deepest_alerting_service(alerts) == "transaction-service"
    assert match_signature("transaction-service", alerts) is None
    assert correlate(alerts) is None


def test_unexplained_deeper_alert_blocks_a_shallower_match():
    # The database is alerting for a reason no signature explains. Blaming the
    # transaction service's pool would restart the wrong thing, so escalate.
    alerts = incident(*POOL, ("ErrorRateHigh", "database"))
    assert correlate(alerts) is None
    assert "database" in why_unmatched(alerts)


def test_empty_incident_escalates():
    assert correlate([]) is None
    assert why_unmatched([]) == "no firing alerts in this incident"


def test_resolved_alerts_do_not_count():
    alerts = incident(*POOL, status="resolved")
    assert correlate(alerts) is None


def test_resolved_alerts_are_ignored_alongside_firing_ones():
    alerts = incident(*POOL)
    alerts[0]["status"] = "resolved"          # APILatencyHigh resolved
    result = correlate(alerts)
    assert result["root_cause"] == "db_connection_pool_exhaustion"
    assert "APILatencyHigh" in result["missing_symptoms"]


# -- ambiguity and determinism -----------------------------------------------------

def test_ambiguous_incident_picks_pool_when_pool_is_better_supported():
    # Both deciding symptoms fired on transaction-service. Pool has its error
    # rate as well (0.55 + 0.20 = 0.75); memory has nothing beyond its own
    # deciding symptom (0.60). The better-supported cause must win.
    alerts = incident(
        ("DBPoolNearExhaustion", "transaction-service"),
        ("ErrorRateHigh", "transaction-service"),
        ("ContainerMemoryHigh", "transaction-service"),
    )
    result = correlate(alerts)
    assert result["root_cause"] == "db_connection_pool_exhaustion"
    assert result["confidence"] == 0.75


def test_ambiguous_incident_picks_memory_when_memory_is_better_supported():
    # Same two deciding symptoms, but now the restarts and latency that memory
    # pressure produces are present: memory scores 1.0, pool only 0.55+0.15+0.10.
    alerts = incident(
        ("DBPoolNearExhaustion", "transaction-service"),
        ("ContainerMemoryHigh", "transaction-service"),
        ("PodRestartsHigh", "transaction-service"),
        ("APILatencyHigh", "api-gateway"),
    )
    result = correlate(alerts)
    assert result["root_cause"] == "high_memory_usage"
    assert result["confidence"] == 1.0


def test_exact_tie_is_broken_by_cause_name_so_results_are_deterministic():
    # Every consequence present: both causes score 1.0. Name order decides, and
    # it must decide the same way every time.
    alerts = incident(*POOL, ("ContainerMemoryHigh", "transaction-service"))
    results = {correlate(alerts)["root_cause"] for _ in range(5)}
    assert results == {"db_connection_pool_exhaustion"}


def test_match_is_independent_of_alert_order():
    forward = correlate(incident(*POOL))
    backward = correlate(incident(*reversed(POOL)))
    assert forward == backward


def test_explanation_names_the_evidence():
    text = correlate(incident(*POOL))["explanation"]
    assert "transaction-service" in text
    assert "DBPoolNearExhaustion" in text
    assert "100%" in text


def test_match_signature_returns_none_for_a_service_with_no_signatures():
    assert match_signature("frontend", incident(("ErrorRateHigh", "frontend"))) is None


def test_alert_names_extracts_labels():
    assert alert_names(incident(*POOL)) == [name for name, _ in POOL]


# ---------------------------------------------------------------- ambiguity (opt-in safety rule)
TWO_FAULTS = incident(("DBDiskNearFull", "database"), ("ContainerMemoryHigh", "transaction-service"),
                      ("APILatencyHigh", "api-gateway"))


def test_default_behaviour_is_unchanged_two_deciding_symptoms_pick_the_deepest():
    from correlate import ambiguous_causes
    result = correlate(TWO_FAULTS)
    assert result is not None and result["root_cause"] == "db_disk_full"
    assert ambiguous_causes(TWO_FAULTS) == ["db_disk_full", "high_memory_usage"]


def test_with_the_rule_on_two_deciding_symptoms_escalate():
    assert correlate(TWO_FAULTS, escalate_on_ambiguity=True) is None
    reason = why_unmatched(TWO_FAULTS, escalate_on_ambiguity=True)
    assert "2 causes" in reason and "db_disk_full" in reason and "high_memory_usage" in reason


def test_the_rule_does_not_touch_ordinary_incidents():
    for symptoms in ([("DBDiskNearFull", "database"), ("ErrorRateHigh", "transaction-service"),
                      ("APILatencyHigh", "api-gateway")],
                     [("ContainerMemoryHigh", "transaction-service"), ("APILatencyHigh", "api-gateway")]):
        assert correlate(incident(*symptoms), escalate_on_ambiguity=True) == correlate(incident(*symptoms))


def test_a_deciding_symptom_on_the_wrong_service_is_not_counted():
    # DBDiskNearFull is only evidence when the database exports it
    alerts = incident(("DBDiskNearFull", "frontend"), ("ContainerMemoryHigh", "transaction-service"))
    from correlate import ambiguous_causes
    assert ambiguous_causes(alerts) == ["high_memory_usage"]
