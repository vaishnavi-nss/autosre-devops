"""Decide whether the metric scorer backs the rule engine's diagnosis.

The scorer was trained on RCAEval's fault types (cpu, mem, disk I/O, delay, loss), which do
not line up with ours. Only memory pressure maps cleanly: `mem` <-> high_memory_usage. For
the other causes the scorer has no matching fault, so only its choice of *service* is
compared. That is still an independent check - it uses raw metrics, not the alerts the
rules saw - but a weaker one, and the verdict says which kind of agreement it was.
"""

from __future__ import annotations

# Rule-engine cause -> scorer faults that would confirm it. Causes absent here are checked
# on the service alone.
CAUSE_FAULTS = {"high_memory_usage": {"mem"}}

# Causes that are a wrong Kubernetes *state* (a service scaled to zero, a bad rollout, a Service that selects nothing or has the wrong type, a paused
# Deployment, extra replicas). They leave no resource-metric signature the scorer was trained on (a service with no pods has no metrics at all), so asking it
# would only produce a meaningless "disagree". It says so instead, unless the model was trained on state signals and names the cause itself. The dispatcher knows the same list (remediation-dispatcher/app.py STATE_CAUSES) and a test
# keeps the two in step with the rule engine.
STATE_CAUSES = frozenset({
    "database_down", "transaction_service_down", "api_gateway_down",
    "bad_rollout_transaction_service", "bad_rollout_api_gateway",
    "database_unreachable", "transaction_service_unreachable", "api_gateway_unreachable",
    "deployment_paused_transaction_service", "deployment_paused_api_gateway",
    "service_type_changed_database", "service_type_changed_transaction_service", "service_type_changed_api_gateway",
    "replicas_above_baseline"})


def judge(cause: str, service: str, scorer_service: str, scorer_fault: str,
          confidence: float, min_confidence: float, vocabulary=()) -> dict:
    """verdict is "agree", "disagree" or "no_opinion".

    `vocabulary` is the set of faults the scorer can name. A scorer trained on our own incidents
    names the rule engine's causes directly, so a cause in its vocabulary is compared on fault
    type too."""
    base = {"scorer_service": scorer_service, "scorer_fault": scorer_fault,
            "confidence": round(confidence, 3)}
    if cause in STATE_CAUSES and cause not in vocabulary:
        return {**base, "verdict": "no_opinion", "basis": "state fault",
                "reason": "this is a Kubernetes state fault and the scorer was not trained on it; it is not asked"}
    if confidence < min_confidence:
        return {**base, "verdict": "no_opinion",
                "reason": f"scorer confidence {confidence:.2f} is below {min_confidence:.2f}"}
    if scorer_service != service:
        return {**base, "verdict": "disagree",
                "reason": f"metrics point at {scorer_service}, rules named {service}"}
    expected = CAUSE_FAULTS.get(cause)
    if cause in vocabulary:
        expected = {cause}
    if expected is None:
        return {**base, "verdict": "agree", "basis": "service only",
                "reason": f"metrics also point at {service}; no matching fault type to compare"}
    if scorer_fault not in expected:
        return {**base, "verdict": "disagree",
                "reason": f"metrics look like {scorer_fault}, not {sorted(expected)[0]}"}
    return {**base, "verdict": "agree", "basis": "service and fault",
            "reason": f"metrics agree on {service} and on the fault type"}
