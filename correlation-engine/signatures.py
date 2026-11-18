"""Symptom signatures for each known root cause (brief sections 5 and 6).

Step 4 of the algorithm matches the alerts that fired against these
signatures. Step 5 scores the match.

CONFIDENCE MODEL
    The brief suggests `matched / expected`. Over four symptoms that can only
    ever yield 25 / 50 / 75 / 100 %, and it treats the deciding symptom as no
    more informative than a downstream consequence. Instead each symptom carries
    a weight, and

        confidence = sum(weights of symptoms that fired) / sum(all weights)

    Weights sum to 1.0, so the score is still a plain, explainable ratio rather
    than a black-box probability - it can be printed back as a sentence.

    The deciding symptom carries the most weight (0.55-0.60) because it is the
    one that distinguishes this cause from every other. The rest are
    consequences - they fire for several causes and so say little about which
    one it is. The consequence is that a cause is never reported with high
    confidence on consequences alone: that case is blocked outright by
    `required_symptoms`, and a lone deciding symptom scores only 0.55-0.60.
    The dispatcher's confidence gate (default 0.75) therefore escalates those
    to a human instead of acting.

`owner` is the service that exports the deciding metric. Note that
`db_connection_pool_exhaustion` is owned by transaction-service, not by the
database: the connection pool lives on the client side, so the pool gauges are
exported there.

Every alert name below must match an `alert:` in monitoring/alert-rules.yaml,
and every playbook must exist in ansible/playbooks/. Both are enforced by tests.
"""

from __future__ import annotations

SIGNATURES: dict[str, dict] = {
    "db_connection_pool_exhaustion": {
        "owner": "transaction-service",
        # The deciding symptom. If this one did not fire, the cause is not this.
        "required_symptoms": ["DBPoolNearExhaustion"],
        "weights": {
            "DBPoolNearExhaustion": 0.55,
            "ErrorRateHigh": 0.20,
            "PodRestartsHigh": 0.15,
            "APILatencyHigh": 0.10,
        },
        "playbook": "restart_service.yml",
        "description": "The transaction service has exhausted its database connection pool.",
    },
    "high_memory_usage": {
        "owner": "transaction-service",
        "required_symptoms": ["ContainerMemoryHigh"],
        "weights": {
            "ContainerMemoryHigh": 0.60,
            "PodRestartsHigh": 0.25,
            "APILatencyHigh": 0.15,
        },
        "playbook": "scale_replicas.yml",
        "description": "Memory pressure is causing the container to be OOM-killed and restarted.",
    },
    "db_disk_full": {
        "owner": "database",
        "required_symptoms": ["DBDiskNearFull"],
        "weights": {
            "DBDiskNearFull": 0.60,
            "ErrorRateHigh": 0.25,
            "APILatencyHigh": 0.15,
        },
        "playbook": "clear_logs.yml",
        "description": "The database data volume is nearly full, so writes are failing.",
    },
    # ---- added in the completion session: a service that is gone, and a rollout that went wrong
    "database_down": {
        "owner": "database",
        "required_symptoms": ["DeploymentUnavailable"],
        "weights": {"DeploymentUnavailable": 0.60, "ErrorRateHigh": 0.25, "APILatencyHigh": 0.15},
        "playbook": "restore_service.yml",
        "description": "The database has no available replica (scaled to zero or every pod gone).",
    },
    "transaction_service_down": {
        "owner": "transaction-service",
        "required_symptoms": ["DeploymentUnavailable"],
        "weights": {"DeploymentUnavailable": 0.60, "ErrorRateHigh": 0.25, "APILatencyHigh": 0.15},
        "playbook": "restore_service.yml",
        "description": "The transaction service has no available replica.",
    },
    "api_gateway_down": {
        "owner": "api-gateway",
        "required_symptoms": ["DeploymentUnavailable"],
        # The gateway is the front door: with no replica left, nothing exists to count a failed request, so no ErrorRateHigh or APILatencyHigh can ever fire for it
        # (the live run showed the deciding alert alone for 9 minutes). Zero available replicas for 30 seconds is itself the whole evidence. See DEFINITIVE.
        "weights": {"DeploymentUnavailable": 1.0},
        "playbook": "restore_service.yml",
        "description": "The API gateway has no available replica.",
    },
    # ---- a Service whose selector no longer matches its pods: the pods are fine and nobody can reach them
    "database_unreachable": {
        "owner": "database",
        "required_symptoms": ["ServiceHasNoEndpoints"],
        "weights": {"ServiceHasNoEndpoints": 0.60, "ErrorRateHigh": 0.25, "APILatencyHigh": 0.15},
        "playbook": "fix_service_selector.yml",
        "description": "The database pods are available but its Service has no endpoint (the selector does not match).",
    },
    "transaction_service_unreachable": {
        "owner": "transaction-service",
        "required_symptoms": ["ServiceHasNoEndpoints"],
        "weights": {"ServiceHasNoEndpoints": 0.60, "ErrorRateHigh": 0.25, "APILatencyHigh": 0.15},
        "playbook": "fix_service_selector.yml",
        "description": "The transaction service pods are available but its Service has no endpoint.",
    },
    "api_gateway_unreachable": {
        "owner": "api-gateway",
        "required_symptoms": ["ServiceHasNoEndpoints"],
        # As for api_gateway_down: requests never reach the gateway, so it cannot report errors or latency. A Service with no ready endpoint while its Deployment has
        # available pods for a minute is itself the whole evidence. See DEFINITIVE.
        "weights": {"ServiceHasNoEndpoints": 1.0},
        "playbook": "fix_service_selector.yml",
        "description": "The API gateway pods are available but its Service has no endpoint.",
    },
    # ---- configuration drift. Each of these is wrong BY ITSELF (nothing else has to fail for it to be a problem, and the fix is exact and safe), so the deciding symptom is
    # the whole evidence and carries the whole weight. They are listed in DEFINITIVE below, which is the one exception to "a lone deciding symptom stays under the gate".
    "deployment_paused_transaction_service": {
        "owner": "transaction-service",
        "required_symptoms": ["DeploymentPaused"],
        "weights": {"DeploymentPaused": 1.0},
        "playbook": "resume_deployment.yml",
        "description": "The transaction service Deployment is paused, so updates and rollbacks are ignored.",
    },
    "deployment_paused_api_gateway": {
        "owner": "api-gateway",
        "required_symptoms": ["DeploymentPaused"],
        "weights": {"DeploymentPaused": 1.0},
        "playbook": "resume_deployment.yml",
        "description": "The API gateway Deployment is paused.",
    },
    "service_type_changed_database": {
        "owner": "database",
        "required_symptoms": ["ServiceTypeChanged"],
        "weights": {"ServiceTypeChanged": 1.0},
        "playbook": "restore_service_type.yml",
        "description": "The database Service is no longer ClusterIP.",
    },
    "service_type_changed_transaction_service": {
        "owner": "transaction-service",
        "required_symptoms": ["ServiceTypeChanged"],
        "weights": {"ServiceTypeChanged": 1.0},
        "playbook": "restore_service_type.yml",
        "description": "The transaction service's Service is no longer ClusterIP.",
    },
    "service_type_changed_api_gateway": {
        "owner": "api-gateway",
        "required_symptoms": ["ServiceTypeChanged"],
        "weights": {"ServiceTypeChanged": 1.0},
        "playbook": "restore_service_type.yml",
        "description": "The API gateway's Service is no longer ClusterIP.",
    },
    "replicas_above_baseline": {
        "owner": "transaction-service",
        "required_symptoms": ["ReplicasAboveBaseline"],
        "weights": {"ReplicasAboveBaseline": 1.0},
        "playbook": "restore_baseline_replicas.yml",
        "description": "The transaction service still has extra replicas long after the incident that added them.",
    },
    # As everywhere, the deciding symptom alone stays below the 0.75 gate. A crash-looping revision also restarts its pods (PodRestartsHigh), so it
    # reaches 1.0 and is rolled back by itself; a revision that only fails its readiness probe or its image pull never restarts, stays at 0.60 and
    # is left to a person.
    "bad_rollout_transaction_service": {
        "owner": "transaction-service",
        "required_symptoms": ["RolloutStalled"],
        "weights": {"RolloutStalled": 0.60, "PodRestartsHigh": 0.40},
        "playbook": "rollback_deployment.yml",
        "description": "A new revision of the transaction service never became ready (bad configuration, bad image or failing probe).",
    },
    "bad_rollout_api_gateway": {
        "owner": "api-gateway",
        "required_symptoms": ["RolloutStalled"],
        "weights": {"RolloutStalled": 0.60, "PodRestartsHigh": 0.40},
        "playbook": "rollback_deployment.yml",
        "description": "A new revision of the API gateway never became ready.",
    },
}

#: Causes whose single deciding symptom is a definitive state: the only ones allowed to clear the confidence gate on that symptom alone. The six drift causes are wrong by
#: themselves; the two gateway causes cannot have a second symptom, because a gateway with no pods or no endpoint cannot report its own failures.
DEFINITIVE = frozenset({"deployment_paused_transaction_service", "deployment_paused_api_gateway", "service_type_changed_database",
                        "service_type_changed_transaction_service", "service_type_changed_api_gateway", "replicas_above_baseline",
                        "api_gateway_down", "api_gateway_unreachable"})

# `expected_symptoms` is derived from the weights, never written by hand, so the
# two can never drift apart.
for _spec in SIGNATURES.values():
    _spec["expected_symptoms"] = list(_spec["weights"])


def signatures_for(service: str) -> dict[str, dict]:
    """Return the signatures whose owning service is `service`."""
    return {
        cause: spec
        for cause, spec in SIGNATURES.items()
        if spec["owner"] == service
    }
