import json
import logging
import os
import subprocess
from pathlib import Path

import yaml
from fastapi import FastAPI, HTTPException, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

app = FastAPI(title="AutoSRE Remediation Dispatcher")
log = logging.getLogger("remediation-dispatcher")

ANSIBLE_DIR = Path("/app/ansible")
MAP_FILE = ANSIBLE_DIR / "remediation_map.yml"
MIN_CONFIDENCE = float(os.getenv("MIN_CONFIDENCE", "0.75"))
AUTO_REMEDIATE = os.getenv("AUTO_REMEDIATE", "false").lower() == "true"

# Only playbooks that have been implemented and reviewed.
ALLOWED_PLAYBOOKS = {"restart_service.yml", "scale_replicas.yml"}


@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/health")
def health():
    return {"status": "healthy", "auto_remediate": AUTO_REMEDIATE}


@app.post("/dispatch")
def dispatch(payload: dict):
    root_cause = payload.get("root_cause")
    confidence = payload.get("confidence")

    if not root_cause:
        return {"status": "escalated", "reason": "Missing root cause"}

    try:
        confidence = float(confidence)
    except (TypeError, ValueError):
        return {"status": "escalated", "reason": "Missing or invalid confidence"}

    if confidence < MIN_CONFIDENCE:
        return {
            "status": "escalated",
            "reason": "Confidence below threshold",
            "confidence": confidence,
            "threshold": MIN_CONFIDENCE,
        }

    with MAP_FILE.open() as file:
        config = yaml.safe_load(file) or {}

    remediation = config.get("remediations", {}).get(root_cause)

    if not remediation:
        return {
            "status": "escalated",
            "reason": "No remediation mapping for root cause",
            "root_cause": root_cause,
        }

    playbook = remediation.get("playbook")
    extra_vars = remediation.get("extra_vars", {})

    if playbook not in ALLOWED_PLAYBOOKS:
        return {
            "status": "escalated",
            "reason": "Playbook is not approved for execution",
            "playbook": playbook,
        }

    playbook_path = ANSIBLE_DIR / "playbooks" / playbook
    if not playbook_path.is_file():
        raise HTTPException(status_code=500, detail="Approved playbook not found")

    if not AUTO_REMEDIATE:
        return {
            "status": "dry_run",
            "root_cause": root_cause,
            "playbook": playbook,
            "extra_vars": extra_vars,
            "message": "Remediation is mapped but execution is disabled",
        }

    command = [
        "ansible-playbook",
        "-i", "inventory/localhost.yml",
        f"playbooks/{playbook}",
        "--extra-vars", json.dumps(extra_vars),
    ]

    try:
        result = subprocess.run(
            command,
            cwd=ANSIBLE_DIR,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"status": "failed", "reason": "Playbook execution timed out"}

    if result.returncode != 0:
        log.error("Ansible failed: %s", result.stderr)
        return {
            "status": "failed",
            "return_code": result.returncode,
            "stderr": result.stderr[-2000:],
        }

    return {
        "status": "executed",
        "root_cause": root_cause,
        "playbook": playbook,
        "output": result.stdout[-2000:],
    }
