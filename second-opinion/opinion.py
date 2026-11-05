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


def judge(cause: str, service: str, scorer_service: str, scorer_fault: str,
          confidence: float, min_confidence: float) -> dict:
    """verdict is "agree", "disagree" or "no_opinion"."""
    base = {"scorer_service": scorer_service, "scorer_fault": scorer_fault,
            "confidence": round(confidence, 3)}
    if confidence < min_confidence:
        return {**base, "verdict": "no_opinion",
                "reason": f"scorer confidence {confidence:.2f} is below {min_confidence:.2f}"}
    if scorer_service != service:
        return {**base, "verdict": "disagree",
                "reason": f"metrics point at {scorer_service}, rules named {service}"}
    expected = CAUSE_FAULTS.get(cause)
    if expected is None:
        return {**base, "verdict": "agree", "basis": "service only",
                "reason": f"metrics also point at {service}; no matching fault type to compare"}
    if scorer_fault not in expected:
        return {**base, "verdict": "disagree",
                "reason": f"metrics look like {scorer_fault}, not {sorted(expected)[0]}"}
    return {**base, "verdict": "agree", "basis": "service and fault",
            "reason": f"metrics agree on {service} and on the fault type"}
