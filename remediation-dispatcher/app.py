"""Restricted asynchronous remediation with durable reservations and recovery checks."""
import json
import logging
import math
import os
import sqlite3
import subprocess
import time
from pathlib import Path
from urllib.request import Request, urlopen

import yaml
from fastapi import BackgroundTasks, FastAPI, HTTPException, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Gauge, generate_latest
from state import RemediationState

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("remediation-dispatcher")
app = FastAPI(title="AutoSRE Remediation Dispatcher")
ANSIBLE_DIR = Path(os.getenv("ANSIBLE_DIR", "/app/ansible"))
MAP_FILE = ANSIBLE_DIR / "remediation_map.yml"
AUTO_REMEDIATE = os.getenv("AUTO_REMEDIATE", "false").lower() == "true"
MIN_CONFIDENCE = float(os.getenv("MIN_CONFIDENCE", "0.75"))
COOLDOWN_SECONDS = 600
STATE = RemediationState(Path(os.getenv("REMEDIATION_DB", "/data/remediation.sqlite3")), COOLDOWN_SECONDS)
# Independent of caller input and the broader mapping catalogue.
ROOT_CAUSE, TARGET, NAMESPACE, PLAYBOOK = "db_connection_pool_exhaustion", "transaction-service", "autosre", "restart_service.yml"
SERVICE_URL = "http://transaction-service.autosre.svc.cluster.local:8000"
REGISTRY = CollectorRegistry()
REQUESTS = Counter("remediation_requests_total", "Dispatcher decisions", ["root_cause", "service", "status"], registry=REGISTRY)
SUCCESS = Counter("remediation_success_total", "Rollout and application recovery verified", ["root_cause", "service"], registry=REGISTRY)
FAILURE = Counter("remediation_failure_total", "Failed attempts requiring operator review", ["root_cause", "service"], registry=REGISTRY)
COOLDOWN = Counter("remediation_cooldown_total", "Duplicate attempts prevented", ["root_cause", "service"], registry=REGISTRY)
IN_PROGRESS = Gauge("remediation_in_progress", "Active remediation", ["root_cause", "service"], registry=REGISTRY)
LAST_SUCCESS = Gauge("remediation_last_success_unixtime", "Last verified recovery", ["root_cause", "service"], registry=REGISTRY)
for metric in (SUCCESS, FAILURE, COOLDOWN, IN_PROGRESS, LAST_SUCCESS):
    metric.labels(ROOT_CAUSE, TARGET)


def decision(status, **details):
    REQUESTS.labels(ROOT_CAUSE, TARGET, status).inc()
    response = {"status": status, "root_cause": ROOT_CAUSE, "service": TARGET, **details}
    log.info("decision %s", json.dumps(response))
    return response


def service_request(path, method="GET"):
    with urlopen(Request(SERVICE_URL + path, method=method), timeout=5) as response:
        return json.load(response)


def deployment():
    from kubernetes import client, config
    config.load_incluster_config()
    return client.AppsV1Api().read_namespaced_deployment(TARGET, NAMESPACE)


def rollout_complete(before, after):
    replicas, status = after.spec.replicas, after.status
    return (after.metadata.generation > before.metadata.generation
            and (status.observed_generation or 0) >= after.metadata.generation
            and status.updated_replicas == replicas and status.replicas == replicas
            and status.ready_replicas == replicas and status.available_replicas == replicas)


def execute(job_id):
    """Runs after acceptance; never retries an uncertain or failed action."""
    IN_PROGRESS.labels(ROOT_CAUSE, TARGET).set(1)
    STATE.finish(job_id, "running", {})
    try:
        before, fault = deployment(), service_request("/recovery")
        if not fault.get("pool_exhausted"):
            STATE.finish(job_id, "not_needed", {"reason": "Live pool already healthy; no restart performed"})
            log.info("remediation_not_needed job_id=%s", job_id)
            return
        if before.spec.replicas != 1:
            raise RuntimeError("Controlled recovery requires exactly one transaction-service replica")
        environment = dict(os.environ, ANSIBLE_STDOUT_CALLBACK="default", ANSIBLE_CONFIG=str(ANSIBLE_DIR / "ansible.cfg"))
        result = subprocess.run(
            ["ansible-playbook", "-i", "inventory/localhost.yml", "playbooks/" + PLAYBOOK,
             "--extra-vars", json.dumps({"target_service": TARGET, "k8s_namespace": NAMESPACE, "remediation_id": job_id})],
            cwd=ANSIBLE_DIR, env=environment, capture_output=True, text=True, timeout=210, check=False)
        log.info("ansible_result job_id=%s return_code=%s output=%s", job_id, result.returncode, result.stdout[-10000:])
        if result.returncode:
            raise RuntimeError("Ansible failed: " + (result.stdout + result.stderr)[-8000:])
        after = deployment()
        if not rollout_complete(before, after):
            raise RuntimeError("New Deployment generation is not fully rolled out")
        if after.spec.template.metadata.annotations.get("autosre.io/remediation-id") != job_id:
            raise RuntimeError("Rollout identity does not match this remediation")
        deadline, recovery = time.monotonic() + 30, None
        while time.monotonic() < deadline:
            try:
                candidate = service_request("/recovery")
                if (candidate.get("pool_exhausted") is False and candidate["pool_utilization"] < 0.9
                        and candidate["instance"] != fault["instance"]
                        and service_request("/transaction", "POST").get("status") == "success"):
                    recovery = candidate
                    break
            except (OSError, ValueError, KeyError):
                pass
            time.sleep(2)
        if recovery is None:
            raise RuntimeError("Rollout finished but application pool/transaction recovery was not verified")
        proof = {"previous_generation": before.metadata.generation, "generation": after.metadata.generation,
                 "previous_instance": fault["instance"], "recovery": recovery, "playbook": PLAYBOOK}
        STATE.finish(job_id, "succeeded", proof)
        SUCCESS.labels(ROOT_CAUSE, TARGET).inc()
        LAST_SUCCESS.labels(ROOT_CAUSE, TARGET).set(time.time())
        log.info("remediation_succeeded job_id=%s proof=%s", job_id, json.dumps(proof))
    except Exception as exc:
        STATE.finish(job_id, "failed", {"action": "escalate_to_human", "reason": str(exc)})
        FAILURE.labels(ROOT_CAUSE, TARGET).inc()
        log.error("remediation_failed job_id=%s action=escalate_to_human reason=%s", job_id, exc)
    finally:
        IN_PROGRESS.labels(ROOT_CAUSE, TARGET).set(0)


@app.get("/metrics")
def metrics():
    return Response(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)


@app.get("/health")
def health():
    return {"status": "healthy", "auto_remediate": AUTO_REMEDIATE, "cooldown_seconds": COOLDOWN_SECONDS,
            "approved_root_cause": ROOT_CAUSE, "approved_service": TARGET}


@app.get("/remediations/latest")
def latest_remediation(response: Response):
    response.headers["Cache-Control"] = "no-store"
    try:
        job = STATE.latest()
    except (OSError, ValueError, sqlite3.Error):
        log.exception("Unable to read remediation history")
        raise HTTPException(status_code=503, detail="Remediation history unavailable",
                            headers={"Cache-Control": "no-store"})
    remaining = max(0, math.ceil(job["requested_at"] + COOLDOWN_SECONDS - time.time())) if job else 0
    failed = bool(job and job["status"] == "failed")
    unresolved = bool(job and job["status"] in ("accepted", "running") and remaining == 0)
    return {
        "status": "ok" if job else "empty",
        "remediation": job,
        "settings": {"auto_remediate": AUTO_REMEDIATE, "min_confidence": MIN_CONFIDENCE,
                     "cooldown_seconds": COOLDOWN_SECONDS},
        "cooldown": {"active": bool(job and not failed and remaining > 0),
                     "remaining_seconds": remaining if not failed else 0,
                     "operator_review_required": failed or unresolved},
    }


@app.get("/remediations/{job_id}")
def remediation(job_id: str):
    job = STATE.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown remediation")
    return job


@app.post("/dispatch")
def dispatch(payload: dict, background_tasks: BackgroundTasks):
    if (payload.get("root_cause") != ROOT_CAUSE or payload.get("service", TARGET) != TARGET
            or payload.get("namespace", NAMESPACE) != NAMESPACE):
        return decision("escalated", action="escalate_to_human", reason="Only autosre/transaction-service pool-exhaustion restart is approved")
    try:
        confidence = float(payload.get("confidence"))
    except (TypeError, ValueError):
        confidence = float("nan")
    if not math.isfinite(confidence) or not MIN_CONFIDENCE <= confidence <= 1.0:
        return decision("escalated", action="escalate_to_human", reason="Invalid confidence or confidence below threshold")
    try:
        config = yaml.safe_load(MAP_FILE.read_text()) or {}
        mapping = config.get("remediations", {}).get(ROOT_CAUSE, {})
        if mapping.get("playbook") != PLAYBOOK or mapping.get("extra_vars") != {"target_service": TARGET}:
            raise ValueError("Approved mapping does not match the fixed action")
        if not (ANSIBLE_DIR / "playbooks" / PLAYBOOK).is_file():
            raise ValueError("Approved playbook missing")
        if not AUTO_REMEDIATE:
            return decision("dry_run", playbook=PLAYBOOK, extra_vars={"target_service": TARGET})
        status, job = STATE.reserve(ROOT_CAUSE, TARGET, confidence=confidence, playbook=PLAYBOOK)
    except (OSError, ValueError, sqlite3.Error) as exc:
        return decision("escalated", action="escalate_to_human", reason=str(exc))
    if status == "cooldown":
        COOLDOWN.labels(ROOT_CAUSE, TARGET).inc()
        return decision("cooldown", job_id=job["id"], retry_after_seconds=max(0, int(job["requested_at"] + COOLDOWN_SECONDS - time.time())),
                        reason="Previous attempt reserved this root-cause/service pair")
    if status == "blocked":
        return decision("escalated", job_id=job["id"], action="escalate_to_human", reason="Previous attempt failed or unresolved; operator review required")
    background_tasks.add_task(execute, job["id"])
    return JSONResponse(decision("accepted", job_id=job["id"], playbook=PLAYBOOK), status_code=202)
