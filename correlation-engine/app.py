"""AutoSRE correlation engine - Alertmanager webhook receiver.

Alertmanager posts one grouped incident here (see the route config in
monitoring/kube-prometheus-stack-values.yaml). This service turns that group
of symptoms into a single ranked root cause, which the remediation dispatcher
maps to an Ansible playbook via ansible/remediation_map.yml.

Endpoints:
    POST /alerts   Alertmanager webhook (payload version 4)
    GET  /health   liveness probe
    GET  /ready    readiness probe
    GET  /metrics  Prometheus metrics
"""

import logging
import os

from fastapi import FastAPI, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, generate_latest

from correlate import correlate
from signatures import SIGNATURES

SERVICE_NAME = os.getenv("SERVICE_NAME", "correlation-engine")

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("correlation-engine")

incidents_received_total = Counter(
    "incidents_received_total", "Alertmanager incident groups received.", ["service"]
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
    firing = [a for a in alerts if a.get("status", "firing") == "firing"]
    incidents_received_total.labels(SERVICE_NAME).inc()

    log.info(
        "incident received: group=%s status=%s alerts=%s",
        payload.get("groupKey", "?"),
        payload.get("status", "?"),
        [a.get("labels", {}).get("alertname") for a in firing],
    )

    result = correlate(firing)

    if result is None:
        unmatched_incidents_total.labels(SERVICE_NAME).inc()
        return {
            "root_cause": None,
            "confidence": None,
            "status": "correlation not implemented",
            "received_alerts": len(firing),
        }

    root_causes_identified_total.labels(SERVICE_NAME, result["root_cause"]).inc()
    last_confidence.labels(SERVICE_NAME).set(result["confidence"])
    log.info(
        "root cause: %s (%.0f%%) -> %s",
        result["root_cause"],
        result["confidence"] * 100,
        result["playbook"],
    )
    return {"status": "correlated", **result}


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
