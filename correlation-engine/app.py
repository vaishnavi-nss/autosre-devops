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
import httpx
import json
import logging
import os
from collections import deque
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, generate_latest

import second_opinion
from correlate import correlate, deepest_alerting_service, firing_only, why_unmatched
from signatures import SIGNATURES
from transparency import build_variants

SERVICE_NAME = os.getenv("SERVICE_NAME", "correlation-engine")
CAPTURE_PATH = os.getenv("CAPTURE_PATH")  # unset = capture disabled
SECOND_OPINION_URL = os.getenv("SECOND_OPINION_URL")          # unset = no second opinion
SECOND_OPINION_MODE = os.getenv("SECOND_OPINION_MODE", "shadow")   # shadow | gate
# Gate mode can only veto the original resource causes. For a Kubernetes-state cause the evidence is the cluster state itself (the scorer is blind to
# several of these faults and once vetoed a correct database_down live), so its verdict is recorded and shown but never stops the fix, same as in the dispatcher.
GATED_CAUSES = frozenset({"db_connection_pool_exhaustion", "high_memory_usage", "db_disk_full"})
SECOND_OPINION_TIMEOUT = float(os.getenv("SECOND_OPINION_TIMEOUT", "4"))
# Escalate rather than act when the deciding symptoms of two or more causes fire together. Off by default.
ESCALATE_ON_AMBIGUITY = os.getenv("ESCALATE_ON_AMBIGUITY", "false").lower() in ("1", "true", "yes")
# Remediation dispatcher configuration
DISPATCHER_URL = os.getenv("DISPATCHER_URL")
DISPATCHER_ENABLED = os.getenv("DISPATCHER_ENABLED", "false").lower() == "true"
DISPATCHER_TIMEOUT = float(os.getenv("DISPATCHER_TIMEOUT", "5"))
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
        cleared = await _auto_resolve(alerts) if payload.get("status") == "resolved" else []
        log.info("resolved notification (%d alert(s)); nothing to diagnose; %d incident(s) closed", len(alerts), len(cleared))
        return {"status": "resolved", "action": "none", "received_alerts": 0, "closed_incidents": cleared}

    incidents_received_total.labels(SERVICE_NAME).inc()

    log.info(
        "incident received: group=%s status=%s alerts=%s",
        payload.get("groupKey", "?"),
        payload.get("status", "?"),
        [a.get("labels", {}).get("alertname") for a in firing],
    )

    result = correlate(firing, ESCALATE_ON_AMBIGUITY)
    rules_only = result if not ESCALATE_ON_AMBIGUITY else correlate(firing, False)     # for transparency mode: what the rules alone would say

    if result is None:
        opinion0 = None
        if rules_only is not None and SECOND_OPINION_URL:                   # ambiguity rule stopped it before the second opinion was asked; ask for the record
            opinion0 = await _second_opinion(rules_only, firing)
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
            "variants": build_variants(firing, rules_only, opinion0, SECOND_OPINION_MODE, ESCALATE_ON_AMBIGUITY),
            "diagnostics": await _request_diagnostics(firing, reason),
        })

    opinion = None
    if SECOND_OPINION_URL:
        opinion = await _second_opinion(result, firing)
        if SECOND_OPINION_MODE == "gate" and opinion["verdict"] == "disagree" and result["root_cause"] in GATED_CAUSES:
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
                "variants": build_variants(firing, rules_only, opinion, SECOND_OPINION_MODE, ESCALATE_ON_AMBIGUITY),
                "diagnostics": await _request_diagnostics(firing, "vetoed by the second opinion"),
            })

    root_causes_identified_total.labels(SERVICE_NAME, result["root_cause"]).inc()
    last_confidence.labels(SERVICE_NAME).set(result["confidence"])
    log.info("root cause: %s (%s)", result["root_cause"], result["explanation"])
    remediation = await _dispatch_remediation(result, opinion)

    response = {"status": "correlated", "received_alerts": len(firing), **result,
                "variants": build_variants(firing, rules_only, opinion, SECOND_OPINION_MODE, ESCALATE_ON_AMBIGUITY)}
    if opinion is not None:
        response["second_opinion"] = opinion
    if remediation is not None:
        response["remediation"] = remediation
        if remediation.get("status") == "escalated":
            response["diagnostics"] = await _request_diagnostics(firing, str(remediation.get("reason") or "escalated by the dispatcher"))
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


async def _request_diagnostics(firing: list, reason: str) -> dict | None:
    """When a person is being asked (an unknown cause, a vetoed diagnosis, an escalation by the dispatcher), have the dispatcher collect evidence about the
    deepest alerting service: pod states, recent events and log tails. Read-only, never blocks and never raises."""
    if not DISPATCHER_ENABLED or not DISPATCHER_URL:
        return None
    service = deepest_alerting_service(firing)
    if not service:
        return None
    url = DISPATCHER_URL.rsplit("/dispatch", 1)[0] + "/diagnostics"
    names = sorted({a.get("labels", {}).get("alertname") for a in firing if a.get("labels", {}).get("alertname")})
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            reply = await client.post(url, json={"service": service, "reason": reason, "alerts": names})
            data = reply.json()
            return {"execution_id": data.get("execution_id"), "service": service, "status": data.get("status")}
    except Exception as exc:                                    # noqa: BLE001
        log.warning("evidence collection not requested: %s", exc)
        return None


async def _dispatch_remediation(result: dict, opinion: dict | None = None) -> dict | None:
    """Send a diagnosis to the dispatcher; never block correlation.

    The dispatcher, not the engine, decides whether to act: it also receives the diagnosed service and the second-opinion
    verdict so that it can require agreement, refuse a service that does not match its playbook, and apply its cooldown."""
    if not DISPATCHER_ENABLED:
        return {"status": "disabled", "reason": "Dispatcher integration is disabled"}

    if not DISPATCHER_URL:
        return {"status": "unavailable", "reason": "DISPATCHER_URL is not configured"}

    try:
        async with httpx.AsyncClient(timeout=DISPATCHER_TIMEOUT) as client:
            response = await client.post(
                DISPATCHER_URL,
                json={
                    "root_cause": result["root_cause"],
                    "confidence": result["confidence"],
                    "service": result.get("service"),
                    "playbook": result.get("playbook"),
                    "second_opinion": opinion,
                },
            )
            response.raise_for_status()
            return response.json()
    except Exception as exc:
        log.error("Remediation dispatcher unavailable: %s", exc)
        return {"status": "unavailable", "reason": str(exc)}


# History. Incidents and events (setting changes, scenario runs) are appended to files on a persistent volume and reloaded after a restart, and
# the lab's sync script copies those files to the S3 bucket, because only the bucket survives a lab restart. Nothing here influences a decision.
INCIDENT_LOG = os.getenv("INCIDENT_LOG", "")
EVENT_LOG = os.getenv("EVENT_LOG", "")
HISTORY_KEEP = int(os.getenv("INCIDENT_KEEP", "500"))
RECENT_INCIDENTS: deque = deque(maxlen=HISTORY_KEEP)      # newest last; read by the front end through GET /incidents
RECENT_EVENTS: deque = deque(maxlen=HISTORY_KEEP)
_seq = {"incident": 0, "event": 0}
EVENT_KEYS = ("t", "setting", "value", "reason", "by", "name", "mode", "scenario", "variant", "result", "text", "started", "finished", "seconds", "playbook", "cause")


def _append(path: str, record: dict) -> None:
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + chr(10))
    except OSError as exc:                                   # a full or read-only volume must never stop the engine
        log.warning("could not persist to %s: %s", path, exc)


def _restore_history() -> None:
    for path, store, key in ((INCIDENT_LOG, RECENT_INCIDENTS, "incident"), (EVENT_LOG, RECENT_EVENTS, "event")):
        if not path or not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                store.append(rec)
                try:
                    _seq[key] = max(_seq[key], int(str(rec.get("id", "")).split("-")[-1]))
                except ValueError:
                    pass
    log.info("restored %d incidents and %d events from the history files", len(RECENT_INCIDENTS), len(RECENT_EVENTS))


def _iso(epoch: float | None) -> str | None:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds") if epoch else None


def _remember(firing: list, response: dict) -> None:
    """Keep what the engine decided about the latest incidents, for the dashboard. Nothing here influences a decision."""
    so = response.get("second_opinion") or {}
    rem = response.get("remediation") or {}
    _seq["incident"] += 1
    record = {
        "id": f"INC-{_seq['incident']:04d}",
        "started_at": _iso(second_opinion.earliest_start(firing)),
        "received_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": response.get("status"),
        "action": response.get("action"),
        "root_cause": response.get("root_cause") or response.get("rule_root_cause"),
        "service": response.get("service"),
        "confidence": response.get("confidence") if response.get("confidence") is not None else response.get("rule_confidence"),
        "playbook": response.get("playbook"),
        "matched_symptoms": response.get("matched_symptoms"),
        "missing_symptoms": response.get("missing_symptoms"),
        "explanation": response.get("explanation"),
        "reason": response.get("reason"),
        "alerts": [{"alertname": a.get("labels", {}).get("alertname"), "service": a.get("labels", {}).get("service")}
                   for a in firing],
        "variants": response.get("variants"),
        "diagnostics": response.get("diagnostics"),
        "second_opinion": {k: so.get(k) for k in ("verdict", "reason", "scorer_service", "scorer_fault", "confidence")} if so else None,
        "remediation": {k: rem.get(k) for k in ("status", "reason", "playbook", "target_service", "execution_id",
                                                 "cooldown_remaining_s", "second_opinion")} if rem else None,
    }
    RECENT_INCIDENTS.append(record)
    _append(INCIDENT_LOG, record)


async def _auto_resolve(alerts: list) -> list[str]:
    """Close incidents whose alerts have all cleared.

    Alertmanager sets the group's status to "resolved" only when every alert in the group has stopped firing. Every open incident whose alerts are all among the
    ones that just cleared is then over, whether it was fixed by a playbook or went away by itself; it is recorded as a normal `resolve` event by "Alertmanager",
    which is how an early partial notification (one generic alert, sent before the deciding alert) stops sitting in the on-call queue for ever. An incident that
    is already resolved is left alone, and nothing here influences a decision."""
    cleared_names = {a.get("labels", {}).get("alertname") for a in alerts}
    done = {e.get("name") for e in RECENT_EVENTS if e.get("kind") == "resolve"}
    closed = []
    for inc in list(RECENT_INCIDENTS):
        names = {a.get("alertname") for a in inc.get("alerts") or []}
        if inc.get("id") in done or not names or not names <= cleared_names:
            continue
        await add_event({"kind": "resolve", "name": inc["id"], "by": "Alertmanager", "text": "every alert of this incident has cleared"})
        closed.append(inc["id"])
    return closed


@app.get("/incidents")
async def incidents(limit: int = 20):
    """The most recent incident decisions, newest first (what the dashboard shows)."""
    items = list(RECENT_INCIDENTS)[-max(1, min(limit, RECENT_INCIDENTS.maxlen or 50)):]
    return {"incidents": list(reversed(items))}


@app.post("/events", status_code=201)
async def add_event(body: dict):
    """Record a setting change or a scenario run (sent by the fault console, the runner and the deploy pipeline). Whitelisted fields only."""
    kind = body.get("kind")
    if kind not in ("change", "run", "ack", "resolve"):
        raise HTTPException(status_code=422, detail="kind must be 'change', 'run', 'ack' or 'resolve'")
    if len(json.dumps(body)) > 4096:
        raise HTTPException(status_code=413, detail="event too large")
    _seq["event"] += 1
    record = {"id": f"EVT-{_seq['event']:04d}", "kind": kind, **{k: body[k] for k in EVENT_KEYS if k in body and isinstance(body[k], (str, int, float, bool, type(None)))}}
    record.setdefault("t", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    RECENT_EVENTS.append(record)
    _append(EVENT_LOG, record)
    return record


@app.get("/events")
async def events(kind: str | None = None, limit: int = 100):
    items = [e for e in RECENT_EVENTS if kind is None or e.get("kind") == kind]
    return {"events": list(reversed(items[-max(1, min(limit, RECENT_EVENTS.maxlen or 100)):]))}


def _capture(payload: dict, response: dict) -> dict:
    """Append the incident and what the engine decided to CAPTURE_PATH, if capture is on.

    Returns `response` unchanged so a handler can `return _capture(payload, response)`.

    Notifications with nothing firing are skipped: when alerts clear, Alertmanager
    sends a "resolved" notification, which is the end of an incident rather than
    an incident, and recording it would score as a missed detection.
    """
    firing = firing_only(payload.get("alerts", []))
    if firing:
        _remember(firing, response)
    if not CAPTURE_PATH or not firing:
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


_restore_history()                       # after every definition above exists
