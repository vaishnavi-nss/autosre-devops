"""AutoSRE correlation engine - Alertmanager webhook receiver.

Alertmanager posts one grouped incident here (see the route config in
monitoring/kube-prometheus-stack-values.yaml). This service turns that group
of symptoms into a single ranked root cause, which the remediation dispatcher
maps to an Ansible playbook via ansible/remediation_map.yml.

Endpoints:
    POST /alerts           Alertmanager webhook (payload version 4)
    GET  /causes           the known root-cause labels
    PUT  /capture/label    set the ground-truth label for captured incidents
                           (only when CAPTURE_PATH is set)
    GET  /health           liveness probe
    GET  /ready            readiness probe
    GET  /metrics          Prometheus metrics

Second opinion (optional): set SECOND_OPINION_URL and each diagnosis is checked against the
raw metrics by the second-opinion service. SECOND_OPINION_MODE=shadow (the default) only
annotates the response and counts the verdicts; =gate also withholds the action - the
incident escalates to a human - when the metrics disagree. A scorer that is down, slow or
unsure never blocks anything.

Evaluation capture: set CAPTURE_PATH to a file path and every incident received
is appended to it as one JSON line, tagged with the current ground-truth label.
Run a fault injection, PUT the label first, and the resulting file is a labelled
dataset for evaluation/evaluate.py --replay. Off by default.
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, generate_latest

import second_opinion
from correlate import correlate, firing_only, why_unmatched
from signatures import SIGNATURES

SERVICE_NAME = os.getenv("SERVICE_NAME", "correlation-engine")
CAPTURE_PATH = os.getenv("CAPTURE_PATH")  # unset = capture disabled
SECOND_OPINION_URL = os.getenv("SECOND_OPINION_URL")          # unset = no second opinion
SECOND_OPINION_MODE = os.getenv("SECOND_OPINION_MODE", "shadow")   # shadow | gate
SECOND_OPINION_TIMEOUT = float(os.getenv("SECOND_OPINION_TIMEOUT", "4"))
# Escalate rather than act when the deciding symptoms of two or more causes fire together. Off by default.
ESCALATE_ON_AMBIGUITY = os.getenv("ESCALATE_ON_AMBIGUITY", "false").lower() in ("1", "true", "yes")

# Ground truth for incidents captured from now on. Module state on purpose: this
# is experiment tooling for one operator, not part of the decision path.
_capture_label: str | None = None

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("correlation-engine")

incidents_received_total = Counter(
    "incidents_received_total",
    "Alertmanager notifications that carried at least one FIRING alert.",
    ["service"],
)
resolved_notifications_total = Counter(
    "resolved_notifications_total",
    "Alertmanager notifications with nothing firing (an incident ending, not starting).",
    ["service"],
)
root_causes_identified_total = Counter(
    "root_causes_identified_total",
    "Incidents resolved to a named root cause.",
    ["service", "root_cause"],
)
unmatched_incidents_total = Counter(
    "unmatched_incidents_total",
    "Incidents with no matching signature - these escalate to a human.",
    ["service"],
)
second_opinion_verdicts_total = Counter(
    "second_opinion_verdicts_total",
    "What the metric scorer said about each diagnosis: agree, disagree, no_opinion, unavailable.",
    ["service", "verdict"],
)
vetoed_incidents_total = Counter(
    "vetoed_incidents_total",
    "Diagnoses withheld because the metric scorer disagreed (gate mode only); these escalate.",
    ["service"],
)
# Exported so Grafana can chart the confidence score, per brief section 4.
last_confidence = Gauge(
    "correlation_last_confidence",
    "Confidence of the most recent root-cause match, 0.0 to 1.0.",
    ["service"],
)

app = FastAPI(title="AutoSRE correlation engine")


@app.get("/metrics")
async def metrics():
    """Serve metrics directly rather than mounting a sub-app.

    A mounted sub-app answers /metrics with a 307 redirect to /metrics/, which
    every scraper then has to follow.
    """
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/alerts", status_code=202)
async def receive_alerts(payload: dict):
    """Receive one grouped incident from Alertmanager."""
    alerts = payload.get("alerts", [])
    firing = firing_only(alerts)

    if not firing:
        # When alerts clear, Alertmanager sends a "resolved" notification. That is the end
        # of an incident, not an incident: counting it inflated `incidents_received_total`
        # and, worse, `unmatched_incidents_total` - the dashboard's "escalated to a human"
        # read 48 after a run that produced only a handful of real escalations.
        resolved_notifications_total.labels(SERVICE_NAME).inc()
        log.info("resolved notification (%d alert(s)); nothing to diagnose", len(alerts))
        return {"status": "resolved", "action": "none", "received_alerts": 0}

    incidents_received_total.labels(SERVICE_NAME).inc()

    log.info(
        "incident received: group=%s status=%s alerts=%s",
        payload.get("groupKey", "?"),
        payload.get("status", "?"),
        [a.get("labels", {}).get("alertname") for a in firing],
    )

    result = correlate(firing, ESCALATE_ON_AMBIGUITY)

    if result is None:
        reason = why_unmatched(firing, ESCALATE_ON_AMBIGUITY)
        unmatched_incidents_total.labels(SERVICE_NAME).inc()
        log.warning("no root cause, escalating to a human: %s", reason)
        return _capture(payload, {
            "root_cause": None,
            "confidence": None,
            "status": "unmatched",
            "action": "escalate_to_human",
            "reason": reason,
            "received_alerts": len(firing),
        })

    opinion = None
    if SECOND_OPINION_URL:
        opinion = await _second_opinion(result, firing)
        if SECOND_OPINION_MODE == "gate" and opinion["verdict"] == "disagree":
            vetoed_incidents_total.labels(SERVICE_NAME).inc()
            log.warning("second opinion disagrees, escalating: %s", opinion.get("reason"))
            return _capture(payload, {
                "root_cause": None,
                "confidence": None,
                "status": "vetoed",
                "action": "escalate_to_human",
                "reason": f"the rules said {result['root_cause']} but the metrics disagree: "
                          f"{opinion.get('reason')}",
                "rule_root_cause": result["root_cause"],
                "rule_confidence": result["confidence"],
                "second_opinion": opinion,
                "received_alerts": len(firing),
            })

    root_causes_identified_total.labels(SERVICE_NAME, result["root_cause"]).inc()
    last_confidence.labels(SERVICE_NAME).set(result["confidence"])
    log.info("root cause: %s (%s)", result["root_cause"], result["explanation"])
    response = {"status": "correlated", "received_alerts": len(firing), **result}
    if opinion is not None:
        response["second_opinion"] = opinion
    return _capture(payload, response)


async def _second_opinion(result: dict, firing: list) -> dict:
    """Ask the metric scorer, off the event loop. Never raises: failures are `unavailable`."""
    deciding = SIGNATURES.get(result["root_cause"], {}).get("required_symptoms", [])
    fault_start = second_opinion.estimate_fault_start(firing, deciding)
    if fault_start is None:
        opinion = {"verdict": "unavailable", "reason": "alerts carry no start time"}
    else:
        opinion = await asyncio.to_thread(
            second_opinion.ask, SECOND_OPINION_URL, result["root_cause"], result["service"],
            fault_start, SECOND_OPINION_TIMEOUT, fault_start)
    second_opinion_verdicts_total.labels(result["service"], opinion["verdict"]).inc()
    log.info("second opinion on %s: %s (%s)", result["root_cause"], opinion["verdict"], opinion.get("reason"))
    return opinion


def _capture(payload: dict, response: dict) -> dict:
    """Append the incident and what the engine decided to CAPTURE_PATH, if capture is on.

    Returns `response` unchanged so a handler can `return _capture(payload, response)`.

    Notifications with nothing firing are skipped: when alerts clear, Alertmanager
    sends a "resolved" notification, which is the end of an incident rather than
    an incident, and recording it would score as a missed detection.
    """
    if not CAPTURE_PATH or not firing_only(payload.get("alerts", [])):
        return response
    record = {
        "label": _capture_label,
        "received_at": datetime.now(timezone.utc).isoformat(),
        "payload": payload,
        # What the engine did with it, including the second opinion, so a run can be scored
        # against ground truth without replaying the webhook.
        "response": {k: response.get(k) for k in
                     ("status", "action", "root_cause", "service", "confidence", "second_opinion",
                      "rule_root_cause")},
    }
    with open(CAPTURE_PATH, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")
    return response


@app.put("/capture/label")
async def set_capture_label(body: dict):
    """Set the ground-truth label stamped on every incident captured from now on.

    Use the root-cause label of the fault you are about to inject, or null for an
    incident the engine should escalate. Disabled unless CAPTURE_PATH is set.
    """
    global _capture_label
    if not CAPTURE_PATH:
        raise HTTPException(status_code=404, detail="capture is not enabled")
    label = body.get("label")
    if label is not None and label not in SIGNATURES:
        raise HTTPException(
            status_code=422,
            detail=f"unknown label {label!r}; expected one of {sorted(SIGNATURES)} or null",
        )
    _capture_label = label
    return {"label": _capture_label, "capture_path": CAPTURE_PATH}


@app.get("/causes")
async def causes():
    """The signature table, handy for debugging and for the demo."""
    return {"causes": sorted(SIGNATURES)}


@app.get("/health")
async def health():
    return {"status": "ok", "service": SERVICE_NAME}


@app.get("/ready")
async def ready():
    return {"status": "ready", "service": SERVICE_NAME}
