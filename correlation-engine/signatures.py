"""Symptom signatures for each known root cause (brief sections 5 and 6).

Step 4 of the algorithm matches the alerts that fired against these
signatures. Step 5 scores the match as `matched / expected`.

`owner` is the service that exports the deciding metric. Note that
`db_connection_pool_exhaustion` is owned by transaction-service, not by the
database: the connection pool lives on the client side, so the pool gauges are
exported there.

Every alert name below must match an `alert:` in monitoring/alert-rules.yaml,
and every playbook must exist in ansible/playbooks/.
"""

from __future__ import annotations

SIGNATURES: dict[str, dict] = {
    "db_connection_pool_exhaustion": {
        "owner": "transaction-service",
        # The deciding symptom. If this one did not fire, the cause is not this.
        "required_symptoms": ["DBPoolNearExhaustion"],
        # Everything this cause is expected to produce, used for the score.
        "expected_symptoms": [
            "DBPoolNearExhaustion",
            "ErrorRateHigh",
            "PodRestartsHigh",
            "APILatencyHigh",
        ],
        "playbook": "restart_service.yml",
        "description": "The transaction service has exhausted its database connection pool.",
    },
    "high_memory_usage": {
        "owner": "transaction-service",
        "required_symptoms": ["ContainerMemoryHigh"],
        "expected_symptoms": [
            "ContainerMemoryHigh",
            "PodRestartsHigh",
            "APILatencyHigh",
        ],
        "playbook": "scale_replicas.yml",
        "description": "Memory pressure is causing the container to be OOM-killed and restarted.",
    },
    "db_disk_full": {
        "owner": "database",
        "required_symptoms": ["DBDiskNearFull"],
        "expected_symptoms": [
            "DBDiskNearFull",
            "ErrorRateHigh",
            "APILatencyHigh",
        ],
        "playbook": "clear_logs.yml",
        "description": "The database data volume is nearly full, so writes are failing.",
    },
}


def signatures_for(service: str) -> dict[str, dict]:
    """Return the signatures whose owning service is `service`."""
    return {
        cause: spec
        for cause, spec in SIGNATURES.items()
        if spec["owner"] == service
    }
