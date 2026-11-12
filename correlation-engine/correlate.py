"""The correlation algorithm (brief section 5).

    1. Time-window grouping      - done by Alertmanager, see below
    2. Dependency graph lookup   - map each alert's `service` label to the chain
    3. Root cause ranking        - the deepest alerting service wins
    4. Symptom-signature match   - which named cause fits the alerts that fired
    5. Confidence score          - weighted share of the expected symptoms seen

Pure functions over a list of Alertmanager alert dicts. No I/O, no globals, no
clock - the same input always gives the same output, which is what makes the
decision auditable after the fact.

FAILING SAFE
    `correlate()` returns None whenever it cannot name a cause, and None means
    "escalate to a human" (brief section 6) - never "try something". Notably, if
    the deepest alerting service has no matching signature the engine does NOT
    fall back to a shallower service that does match. A deeper service alerting
    for an unknown reason is the likeliest true cause, and acting on a shallower
    one would restart the wrong thing.

On step 1: Alertmanager already buckets alerts by time. The route in
monitoring/kube-prometheus-stack-values.yaml sets `group_wait: 30s`, so this
service receives one webhook per incident rather than one per alert. Do not
re-implement grouping here unless that config is removed.
"""

from __future__ import annotations

from dependency_graph import depth, is_known
from signatures import SIGNATURES, signatures_for


def firing_only(alerts: list[dict]) -> list[dict]:
    """Drop alerts that have already resolved."""
    return [a for a in alerts if a.get("status", "firing") == "firing"]


def alert_names(alerts: list[dict]) -> list[str]:
    """Return the `alertname` label of each alert."""
    return [a.get("labels", {}).get("alertname", "") for a in alerts]


def alerting_services(alerts: list[dict]) -> set[str]:
    """Return the distinct `service` labels present in the alert group.

    The `service` label is the contract with Prometheus - every rule in
    monitoring/alert-rules.yaml sets it.
    """
    return {
        a.get("labels", {}).get("service", "")
        for a in alerts
        if a.get("labels", {}).get("service")
    }


def services_by_depth(alerts: list[dict]) -> list[str]:
    """Step 2: alerting services that are in the chain, deepest first.

    Services outside the modelled chain are ignored - the engine has no idea
    where they sit, so it must not reason about them.
    """
    known = [s for s in alerting_services(alerts) if is_known(s)]
    return sorted(known, key=depth)


def deepest_alerting_service(alerts: list[dict]) -> str | None:
    """Step 3: the alerting service closest to the database.

    Problems cascade upward through the chain, so the deepest service that is
    alerting is the one nothing else can explain.
    """
    ordered = services_by_depth(alerts)
    return ordered[0] if ordered else None


def confidence(cause: str, alerts: list[dict]) -> float:
    """Step 5: weighted share of this cause's expected symptoms that fired.

    A plain explainable ratio rather than a black-box probability - see the
    CONFIDENCE MODEL note in signatures.py. Symptoms count wherever in the
    chain they fired, because consequences surface upstream of the cause
    (a database fault raises latency at the gateway).

    Returns 0.0 to 1.0, rounded to four places.
    """
    weights = SIGNATURES[cause]["weights"]
    fired = set(alert_names(alerts))
    matched = sum(w for symptom, w in weights.items() if symptom in fired)
    return round(matched / sum(weights.values()), 4)


def match_signature(service: str, alerts: list[dict]) -> str | None:
    """Step 4: the root-cause label whose signature fits these alerts.

    A signature matches only if every one of its `required_symptoms` fired *on
    the owning service itself*. The deciding metric is exported by the owner,
    so the same alert name appearing on a different service is a coincidence,
    not evidence.

    If several signatures match, the highest-scoring wins; ties are broken by
    name so the result is deterministic.
    """
    fired_here = {
        a.get("labels", {}).get("alertname", "")
        for a in alerts
        if a.get("labels", {}).get("service") == service
    }

    candidates = [
        cause
        for cause, spec in signatures_for(service).items()
        if set(spec["required_symptoms"]) <= fired_here
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda c: (-confidence(c, alerts), c))


def ambiguous_causes(alerts: list[dict]) -> list[str]:
    """Every known cause whose deciding symptom fired on the service that owns it.

    One entry is normal. Two or more means two faults at once, or one fault and a coincidence: the engine
    would name only the deepest, and the other may be the real one.
    """
    fired = {
        (a.get("labels", {}).get("service"), a.get("labels", {}).get("alertname"))
        for a in firing_only(alerts)
    }
    return sorted(
        cause
        for cause, spec in SIGNATURES.items()
        if all((spec["owner"], symptom) in fired for symptom in spec["required_symptoms"])
    )


def why_unmatched(alerts: list[dict], escalate_on_ambiguity: bool = False) -> str:
    """A one-line reason for returning None, for logs and the webhook response."""
    firing = firing_only(alerts)
    if not firing:
        return "no firing alerts in this incident"
    causes = ambiguous_causes(firing)
    if escalate_on_ambiguity and len(causes) > 1:
        return f"the deciding symptoms of {len(causes)} causes fired together ({', '.join(causes)})"
    service = deepest_alerting_service(firing)
    if service is None:
        return "no alert carries a service label that is part of the dependency chain"
    owned = signatures_for(service)
    if not owned:
        return (
            f"{service} is the deepest alerting service but no known cause is "
            f"owned by it"
        )
    needed = sorted({s for spec in owned.values() for s in spec["required_symptoms"]})
    return (
        f"{service} is the deepest alerting service but none of its deciding "
        f"symptoms fired ({', '.join(needed)})"
    )


def explain(result: dict) -> str:
    """Render a result as the sentence an auditor would want to read."""
    spec = SIGNATURES[result["root_cause"]]
    matched = result["matched_symptoms"]
    return (
        f"{result['service']} is the deepest alerting service in the chain; "
        f"its deciding symptom {', '.join(spec['required_symptoms'])} fired; "
        f"{len(matched)} of {len(spec['weights'])} expected symptoms present "
        f"({', '.join(matched)}); confidence {result['confidence']:.0%}."
    )


def correlate(alerts: list[dict], escalate_on_ambiguity: bool = False) -> dict | None:
    """Run steps 2-5 over one incident's alerts.

    With `escalate_on_ambiguity`, an incident in which the deciding symptoms of two or more causes fired
    returns None instead of the deepest one. Off by default: it is a safety rule, not a measured gain (see
    "Recommendations" in docs/evaluation.md), and it escalates more when two real faults coincide.

    Returns a dict with the keys the dispatcher depends on -
    `root_cause`, `service`, `confidence`, `playbook` - plus `matched_symptoms`,
    `missing_symptoms` and `explanation` for the audit trail. Returns None when
    no root cause can be named, which the dispatcher treats as "escalate".
    """
    firing = firing_only(alerts)

    if escalate_on_ambiguity and len(ambiguous_causes(firing)) > 1:
        return None

    service = deepest_alerting_service(firing)       # steps 2-3
    if service is None:
        return None

    cause = match_signature(service, firing)         # step 4
    if cause is None:
        return None

    spec = SIGNATURES[cause]
    fired = set(alert_names(firing))
    result = {
        "root_cause": cause,
        "service": service,
        "confidence": confidence(cause, firing),     # step 5
        "playbook": spec["playbook"],
        "matched_symptoms": [s for s in spec["weights"] if s in fired],
        "missing_symptoms": [s for s in spec["weights"] if s not in fired],
    }
    result["explanation"] = explain(result)
    return result
