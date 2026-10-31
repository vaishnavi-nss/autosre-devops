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
}

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
