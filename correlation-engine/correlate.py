"""The correlation algorithm (brief section 5).

    1. Time-window grouping      - already done by Alertmanager, see below
    2. Dependency graph lookup   - map each alert's `service` label to the chain
    3. Root cause ranking        - the deepest alerting service wins
    4. Symptom-signature match   - which named cause fits the alerts that fired
    5. Confidence score          - matched / expected symptoms

STATUS: build step 4 of the brief. The data tables in dependency_graph.py and
signatures.py are complete; the functions below are stubs. `correlate()`
returns None until they are implemented, and the webhook in app.py answers
202 either way so the Alertmanager wiring can be tested first.

On step 1: Alertmanager already buckets alerts by time. The route in
monitoring/kube-prometheus-stack-values.yaml sets `group_wait: 30s`, so this
service receives one webhook per incident rather than one per alert. Do not
re-implement grouping here unless that config is removed.
"""

from __future__ import annotations

from dependency_graph import depth
from signatures import SIGNATURES, signatures_for


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


def deepest_alerting_service(alerts: list[dict]) -> str | None:
    """Step 3: the alerting service closest to the database.

    TODO: return the service from `alerting_services()` with the smallest
    `depth()`, ignoring services that are not part of the chain.
    """
    return None


def match_signature(service: str, alerts: list[dict]) -> str | None:
    """Step 4: pick the root-cause label whose signature fits these alerts.

    TODO: for each signature owned by `service`, check that every
    `required_symptoms` entry is present in `alert_names(alerts)`, and return
    the best-scoring match.
    """
    return None


def confidence(cause: str, alerts: list[dict]) -> float:
    """Step 5: matched symptoms over expected symptoms, as a 0.0-1.0 ratio.

    Deliberately a plain explainable ratio rather than a black-box probability.

    TODO: count how many of the signature's `expected_symptoms` appear in
    `alert_names(alerts)` and divide by the number expected.
    """
    return 0.0


def correlate(alerts: list[dict]) -> dict | None:
    """Run steps 2-5 over one incident's alerts.

    Returns None when no root cause can be determined, which the dispatcher
    treats as "escalate to a human" (brief section 6).
    """
    # TODO (build step 4): wire the functions above together, e.g.
    #     service = deepest_alerting_service(alerts)
    #     cause = match_signature(service, alerts) if service else None
    #     return None if cause is None else {
    #         "root_cause": cause,
    #         "service": service,
    #         "confidence": confidence(cause, alerts),
    #         "playbook": SIGNATURES[cause]["playbook"],
    #     }
    return None
