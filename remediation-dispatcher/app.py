"""AutoSRE remediation dispatcher.

Receives the correlation engine's diagnosis and decides whether an approved Ansible playbook may run. Nothing here guesses:
every doubt (low confidence, unknown cause, unapproved playbook, wrong target, disagreeing second opinion, cooldown, a run already
in progress, a failed preflight) ends in `escalated` or a skip, never in an action.

Flow of one request (`POST /dispatch`):

    validate -> confidence >= MIN_CONFIDENCE -> mapped + approved playbook -> target and variables inside the allow-list
             -> second-opinion policy -> AUTO_REMEDIATE? (no: `dry_run`, nothing runs)
             -> duplicate / cooldown guard -> accepted (202) -> background run:
                preflight -> ansible-playbook -> independent verification -> rollback (scale only) -> recorded result

The engine's call has a 5 s timeout, so execution is asynchronous: `/dispatch` returns at once and the run is followed through
`GET /executions` and `GET /executions/{id}`. A run counts as `succeeded` only when Kubernetes and the service's own health check
confirm recovery, not because ansible exited 0.
"""
from __future__ import annotations

import json
import logging
import math
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path

import yaml
from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

app = FastAPI(title="AutoSRE Remediation Dispatcher")
log = logging.getLogger("remediation-dispatcher")

ANSIBLE_DIR = Path(os.getenv("ANSIBLE_DIR", "/app/ansible"))
MAP_FILE = ANSIBLE_DIR / "remediation_map.yml"

# What each approved playbook may touch. A map entry outside this table is refused even if the file says otherwise.
POLICY = {
    "restart_service.yml": {"targets": {"transaction-service"}, "vars": {"target_service"}},
    "scale_replicas.yml": {"targets": {"transaction-service"}, "vars": {"target_service", "replicas", "replace_pods"}},
    "clear_logs.yml": {"targets": {"database"}, "vars": {"target_service"}},
}
ALLOWED_PLAYBOOKS = set(POLICY)               # kept for callers that import it


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return float(default)


def settings() -> dict:
    """Read at call time so a changed environment (and the tests) take effect without a restart."""
    return {
        "auto_remediate": os.getenv("AUTO_REMEDIATE", "false").lower() == "true",
        "min_confidence": _env_float("MIN_CONFIDENCE", 0.75),
        "namespace": os.getenv("TARGET_NAMESPACE", "autosre"),
        "cooldown_s": _env_float("COOLDOWN_SECONDS", 600),
        "second_opinion_policy": os.getenv("SECOND_OPINION_POLICY", "agree"),   # agree | not_disagree | ignore
        "max_replicas": int(_env_float("MAX_REPLICAS", 5)),
        "verify_timeout_s": _env_float("VERIFY_TIMEOUT_SECONDS", 150),
        "ansible_timeout_s": _env_float("ANSIBLE_TIMEOUT_SECONDS", 240),
        "rollback_on_failure": os.getenv("ROLLBACK_ON_FAILURE", "true").lower() == "true",
        "disk_recovered_below": _env_float("DISK_RECOVERED_BELOW", 0.9),
        "service_url_template": os.getenv("SERVICE_URL_TEMPLATE", "http://{service}.{namespace}.svc.cluster.local:8000"),
        "exec_log": os.getenv("EXEC_LOG", ""),
    }


# ------------------------------------------------------------------------------------------------ metrics
decisions_total = Counter("remediation_decisions_total", "Dispatch decisions.", ["status", "root_cause"])
skipped_total = Counter("remediation_skipped_total", "Remediations not run, by reason.", ["reason"])
executions_total = Counter("remediation_executions_total", "Finished executions.", ["playbook", "result"])
in_progress = Gauge("remediation_in_progress", "Executions currently running.")
duration_seconds = Histogram("remediation_duration_seconds", "Execution duration.", ["playbook"],
                             buckets=(5, 15, 30, 60, 120, 240, 600))
last_success = Gauge("remediation_last_success_timestamp", "Unix time of the last verified recovery.", ["playbook"])

# ------------------------------------------------------------------------------------------------ state
_lock = threading.Lock()
_executions: "OrderedDict[str, dict]" = OrderedDict()      # newest last
_cooldowns: dict = {}                                        # (cause, target) -> monotonic time the cooldown ends
_inflight: set = set()
MAX_KEPT = 100


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------------------------------------------ cluster access
SA_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")


class Cluster:
    """Everything the verification needs from Kubernetes and the services. Replaced by a fake in tests.

    The API calls are plain REST with the pod's service-account token. The `kubernetes` Python client (36.0.0 on the real cluster)
    sent every request as system:anonymous while a raw call with the same token returned 200, so verification must not depend on it."""

    def _api(self, method: str, path: str, body: dict | None = None, content_type: str = "application/json") -> dict:
        import ssl
        host = os.environ.get("KUBERNETES_SERVICE_HOST")
        if not host:
            raise RuntimeError("not running inside a cluster (KUBERNETES_SERVICE_HOST is not set)")
        token = (SA_DIR / "token").read_text().strip()               # re-read each time: the token is rotated
        ctx = ssl.create_default_context(cafile=str(SA_DIR / "ca.crt"))
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"https://{host}:{os.environ.get('KUBERNETES_SERVICE_PORT', '443')}{path}", data=data, method=method,
                                     headers={"Authorization": f"Bearer {token}", "Accept": "application/json", "Content-Type": content_type})
        try:
            with urllib.request.urlopen(req, context=ctx, timeout=10) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"{method} {path} -> HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:200]}") from exc

    def deployment(self, name: str, namespace: str) -> dict:
        d = self._api("GET", f"/apis/apps/v1/namespaces/{namespace}/deployments/{name}")
        s = d.get("status", {})
        return {"generation": d["metadata"].get("generation", 0), "observed": s.get("observedGeneration", 0),
                "replicas": d["spec"].get("replicas", 0), "updated": s.get("updatedReplicas", 0),
                "ready": s.get("readyReplicas", 0), "available": s.get("availableReplicas", 0)}

    def scale(self, name: str, namespace: str, replicas: int) -> None:
        self._api("PATCH", f"/apis/apps/v1/namespaces/{namespace}/deployments/{name}/scale", {"spec": {"replicas": replicas}},
                  "application/merge-patch+json")

    def get(self, url: str, timeout: float = 4.0) -> tuple[int, str]:
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                return r.status, r.read(200_000).decode("utf-8", "replace")
        except Exception as exc:                               # noqa: BLE001
            return 0, str(exc)


cluster = Cluster()


def _ansible_available() -> bool:
    from shutil import which
    return which("ansible-playbook") is not None


# ------------------------------------------------------------------------------------------------ decision
def _verdict_of(payload: dict) -> str | None:
    so = payload.get("second_opinion")
    if isinstance(so, dict):
        return so.get("verdict")
    return so if isinstance(so, str) else None


def decide(payload: dict, cfg: dict | None = None) -> dict:
    """Pure policy check. Returns a decision with `status` in escalated | dry_run | ready (ready = may execute)."""
    cfg = cfg or settings()
    root_cause = payload.get("root_cause")
    if not root_cause or not isinstance(root_cause, str):
        return {"status": "escalated", "reason": "Missing root cause"}
    try:
        confidence = float(payload.get("confidence"))
    except (TypeError, ValueError):
        return {"status": "escalated", "reason": "Missing or invalid confidence", "root_cause": root_cause}
    if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
        return {"status": "escalated", "reason": "Confidence is not a number between 0 and 1", "root_cause": root_cause}
    base = {"root_cause": root_cause, "confidence": confidence}

    if confidence < cfg["min_confidence"]:
        return {**base, "status": "escalated", "reason": "Confidence below threshold", "threshold": cfg["min_confidence"]}

    try:
        with MAP_FILE.open() as fh:
            mapping = (yaml.safe_load(fh) or {}).get("remediations", {})
    except OSError as exc:
        return {**base, "status": "escalated", "reason": f"Remediation map unreadable: {exc}"}
    entry = mapping.get(root_cause)
    if not entry:
        return {**base, "status": "escalated", "reason": "No remediation mapping for root cause"}

    playbook = entry.get("playbook")
    if playbook not in POLICY:
        return {**base, "status": "escalated", "reason": "Playbook is not approved for execution", "playbook": playbook}
    if not (ANSIBLE_DIR / "playbooks" / playbook).is_file():
        return {**base, "status": "escalated", "reason": "Approved playbook file is missing", "playbook": playbook}

    extra = dict(entry.get("extra_vars") or {})
    unknown = set(extra) - POLICY[playbook]["vars"]
    if unknown:
        return {**base, "status": "escalated", "reason": f"Variables not allowed for {playbook}: {sorted(unknown)}", "playbook": playbook}
    target = extra.get("target_service")
    if target not in POLICY[playbook]["targets"]:
        return {**base, "status": "escalated", "reason": f"{target!r} is not an approved target for {playbook}", "playbook": playbook}
    if "replicas" in extra:
        try:
            replicas = int(extra["replicas"])
        except (TypeError, ValueError):
            return {**base, "status": "escalated", "reason": "replicas is not an integer", "playbook": playbook}
        if not 1 <= replicas <= cfg["max_replicas"]:
            return {**base, "status": "escalated", "reason": f"replicas must be 1 to {cfg['max_replicas']}", "playbook": playbook}
        extra["replicas"] = replicas

    if "replace_pods" in extra and not isinstance(extra["replace_pods"], bool):
        return {**base, "status": "escalated", "reason": "replace_pods must be true or false", "playbook": playbook}

    service = payload.get("service")
    if service and service != target:
        return {**base, "status": "escalated", "reason": f"Diagnosed service {service!r} differs from the playbook target {target!r}",
                "playbook": playbook}

    verdict, policy = _verdict_of(payload), cfg["second_opinion_policy"]
    if policy == "agree" and verdict != "agree":
        return {**base, "status": "escalated", "reason": f"Second opinion is {verdict or 'missing'}, policy requires agree",
                "playbook": playbook, "second_opinion": verdict}
    if policy == "not_disagree" and verdict == "disagree":
        return {**base, "status": "escalated", "reason": "Second opinion disagrees", "playbook": playbook, "second_opinion": verdict}

    plan = {"root_cause": root_cause, "confidence": confidence, "playbook": playbook, "target_service": target,
            "namespace": cfg["namespace"], "extra_vars": extra, "second_opinion": verdict}
    if not cfg["auto_remediate"]:
        return {**plan, "status": "dry_run", "message": "All checks passed; execution is disabled (AUTO_REMEDIATE=false)"}
    return {**plan, "status": "ready"}


# ------------------------------------------------------------------------------------------------ execution
def _step(record: dict, step: str, detail: str = "") -> None:
    record["steps"].append({"t": _now(), "step": step, "detail": detail})
    log.info("[%s] %s %s", record["id"], step, detail)


def _persist(record: dict, cfg: dict) -> None:
    if cfg["exec_log"]:
        try:
            with open(cfg["exec_log"], "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record) + "\n")
        except OSError as exc:
            log.warning("could not write execution log: %s", exc)


def _load_history(cfg: dict) -> int:
    """Reload finished executions from EXEC_LOG after a restart, so the dashboard keeps its history, and re-arm any cooldown that has not
    run out (a restart must not let the same fix run again straight away). Unreadable lines are skipped. Returns how many were loaded."""
    path = cfg["exec_log"]
    if not path or not Path(path).is_file():
        return 0
    records = []
    try:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict) and rec.get("id") and rec.get("started_at"):
                records.append(rec)
    except OSError as exc:
        log.warning("could not read execution log: %s", exc)
        return 0
    records = records[-MAX_KEPT:]
    now = datetime.now(timezone.utc)
    with _lock:
        for rec in records:
            _executions[rec["id"]] = rec
            try:
                finished = datetime.fromisoformat(rec["finished_at"])
            except (KeyError, TypeError, ValueError):
                continue
            remaining = cfg["cooldown_s"] - (now - finished).total_seconds()
            if remaining > 0 and rec.get("status") != "preflight_failed":
                _cooldowns[(rec.get("root_cause"), rec.get("target_service"))] = time.monotonic() + remaining
    log.info("reloaded %d executions from %s", len(records), path)
    return len(records)


@app.on_event("startup")
def _restore_state() -> None:
    _load_history(settings())


def _service_url(cfg: dict, service: str) -> str:
    return cfg["service_url_template"].format(service=service, namespace=cfg["namespace"]).rstrip("/")


def _disk_ratio(cfg: dict, service: str) -> float | None:
    status, body = cluster.get(_service_url(cfg, service) + "/metrics")
    if status != 200:
        return None
    for line in body.splitlines():
        if line.startswith("db_disk_usage_ratio"):
            try:
                return float(line.rsplit(" ", 1)[1])
            except (ValueError, IndexError):
                return None
    return None


def verify(record: dict, before: dict, cfg: dict, sleep=time.sleep) -> tuple[bool, str]:
    """Independent check that the fix worked. Polls until it holds or the timeout passes."""
    target, ns, playbook = record["target_service"], record["namespace"], record["playbook"]
    deadline = time.monotonic() + cfg["verify_timeout_s"]
    why = "not checked"
    while True:
        try:
            now = cluster.deployment(target, ns)
        except Exception as exc:                                # noqa: BLE001
            now, why = None, f"deployment unreadable: {exc}"
        if now is not None:
            settled = now["observed"] >= now["generation"] and now["updated"] == now["replicas"] and \
                now["ready"] == now["replicas"] and now["available"] == now["replicas"]
            if playbook == "restart_service.yml":
                ok = settled and now["generation"] > before["generation"] and now["replicas"] >= 1
                why = "rollout complete" if ok else f"rollout not complete: {now} (generation before {before['generation']})"
            elif playbook == "scale_replicas.yml":
                want = record["extra_vars"]["replicas"]
                ok = settled and now["replicas"] == want
                why = f"{now['ready']}/{want} replicas ready" if not ok else f"{want} replicas ready"
                if ok and record["extra_vars"].get("replace_pods"):
                    # a scale that changes the count is one generation and rolling the pods is another; if the count was already right only
                    # the roll counts. Fewer means the old pods (and any leak in them) are still there.
                    needed = before["generation"] + (2 if before["replicas"] != want else 1)
                    if now["generation"] < needed:
                        ok, why = False, f"the pods were not replaced (generation {before['generation']} -> {now['generation']})"
            else:                                               # clear_logs.yml: the deployment must be untouched and healthy
                ok = settled and now["generation"] == before["generation"]
                why = "deployment healthy" if ok else f"deployment not settled: {now}"
            if ok:
                status, _ = cluster.get(_service_url(cfg, target) + "/health")
                if status != 200:
                    ok, why = False, f"/health answered {status}"
            if ok and playbook == "clear_logs.yml":
                ratio = _disk_ratio(cfg, target)
                if ratio is None:
                    ok, why = False, "disk metric unreadable"
                elif ratio >= cfg["disk_recovered_below"]:
                    ok, why = False, f"disk ratio still {ratio:.2f}"
                else:
                    why = f"disk ratio {ratio:.2f}"
            if ok:
                return True, why
        if time.monotonic() >= deadline:
            return False, why
        sleep(2)


def run_execution(record: dict, sleep=time.sleep) -> dict:
    """Run one approved playbook and verify it. Never raises: every failure is recorded and escalated."""
    cfg = settings()
    key = (record["root_cause"], record["target_service"])
    target, ns, playbook = record["target_service"], record["namespace"], record["playbook"]
    started = time.monotonic()
    ran = False
    try:
        _step(record, "preflight")
        if not _ansible_available():
            raise RuntimeError("ansible-playbook is not installed")
        try:
            before = cluster.deployment(target, ns)
        except Exception as exc:                                # noqa: BLE001
            raise RuntimeError(f"cannot read deployment {ns}/{target}: {exc}") from exc
        if before["replicas"] < 1 and playbook != "scale_replicas.yml":
            raise RuntimeError(f"{target} has no replicas to remediate")
        record["before"] = before

        _step(record, "ansible", playbook)
        env_vars = {**record["extra_vars"], "k8s_namespace": ns}
        command = ["ansible-playbook", "-i", "inventory/localhost.yml", f"playbooks/{playbook}", "--extra-vars", json.dumps(env_vars)]
        ran = True
        try:
            result = subprocess.run(command, cwd=ANSIBLE_DIR, capture_output=True, text=True, timeout=cfg["ansible_timeout_s"], check=False)
        except subprocess.TimeoutExpired:
            record.update(status="failed", reason="Playbook execution timed out")
            _step(record, "ansible_timeout")
            return _finish(record, key, started, "failed", cfg)
        record["return_code"], record["stdout_tail"], record["stderr_tail"] = result.returncode, result.stdout[-1500:], result.stderr[-1500:]
        if result.returncode != 0:
            record.update(status="failed", reason=f"ansible exited {result.returncode}")
            _step(record, "ansible_failed", result.stderr[-300:])
            _maybe_rollback(record, before, cfg)
            return _finish(record, key, started, "failed", cfg)

        _step(record, "verify")
        ok, why = verify(record, before, cfg, sleep)
        try:
            record["after"] = cluster.deployment(target, ns)
        except Exception:                                       # noqa: BLE001
            record["after"] = None
        if ok:
            record.update(status="succeeded", reason=why, recovered=True)
            _step(record, "recovered", why)
            last_success.labels(playbook).set(time.time())
            return _finish(record, key, started, "succeeded", cfg)
        record.update(status="verification_failed", reason=why, recovered=False, escalate=True)
        _step(record, "verification_failed", why)
        _maybe_rollback(record, before, cfg)
        return _finish(record, key, started, "verification_failed", cfg)
    except Exception as exc:                                    # noqa: BLE001 - preflight and anything unexpected
        record.update(status="preflight_failed" if not ran else "failed", reason=str(exc), recovered=False, escalate=True)
        _step(record, record["status"], str(exc))
        return _finish(record, key, started, record["status"], cfg, started_cooldown=ran)


def _maybe_rollback(record: dict, before: dict, cfg: dict) -> None:
    """Only a scale-out is rolled back (to the previous replica count). A restart has nothing to undo."""
    if record["playbook"] != "scale_replicas.yml" or not cfg["rollback_on_failure"]:
        return
    try:
        cluster.scale(record["target_service"], record["namespace"], before["replicas"])
        record["rolled_back_to"] = before["replicas"]
        _step(record, "rollback", f"scaled back to {before['replicas']}")
    except Exception as exc:                                    # noqa: BLE001
        record["rollback_error"] = str(exc)
        _step(record, "rollback_failed", str(exc))


def _finish(record: dict, key: tuple, started: float, result: str, cfg: dict, started_cooldown: bool = True) -> dict:
    record["finished_at"] = _now()
    record["duration_s"] = round(time.monotonic() - started, 1)
    with _lock:
        _inflight.discard(key)
        if started_cooldown and result != "preflight_failed":
            _cooldowns[key] = time.monotonic() + cfg["cooldown_s"]
    in_progress.dec()
    executions_total.labels(record["playbook"], result).inc()
    duration_seconds.labels(record["playbook"]).observe(record["duration_s"])
    _persist(record, cfg)
    return record


def _spawn(record: dict) -> None:
    threading.Thread(target=run_execution, args=(record,), daemon=True, name=f"exec-{record['id']}").start()


launcher = _spawn            # tests replace this with an inline runner


# ------------------------------------------------------------------------------------------------ HTTP
@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/health")
def health():
    cfg = settings()
    return {"status": "healthy", "auto_remediate": cfg["auto_remediate"], "min_confidence": cfg["min_confidence"],
            "second_opinion_policy": cfg["second_opinion_policy"], "cooldown_seconds": cfg["cooldown_s"],
            "approved_playbooks": sorted(POLICY)}


@app.get("/ready")
def ready():
    return {"status": "ready"}


@app.post("/dispatch")
def dispatch(payload: dict):
    cfg = settings()
    decision = decide(payload, cfg)
    label = str(decision.get("root_cause") or payload.get("root_cause") or "none")
    if decision["status"] != "ready":
        decisions_total.labels(decision["status"], label).inc()
        if decision["status"] == "escalated":
            skipped_total.labels("escalated").inc()
        return decision

    key = (decision["root_cause"], decision["target_service"])
    with _lock:
        if key in _inflight:
            decisions_total.labels("duplicate", label).inc(); skipped_total.labels("duplicate").inc()
            return {**decision, "status": "skipped", "reason": "duplicate: a remediation for this cause and service is already running"}
        remaining = _cooldowns.get(key, 0) - time.monotonic()
        if remaining > 0:
            decisions_total.labels("cooldown", label).inc(); skipped_total.labels("cooldown").inc()
            return {**decision, "status": "skipped", "reason": "cooldown", "cooldown_remaining_s": round(remaining)}
        _inflight.add(key)
        record = {"id": uuid.uuid4().hex[:12], "status": "running", "started_at": _now(), "steps": [], "recovered": None,
                  **{k: decision[k] for k in ("root_cause", "confidence", "playbook", "target_service", "namespace", "extra_vars", "second_opinion")}}
        _executions[record["id"]] = record
        while len(_executions) > MAX_KEPT:
            _executions.popitem(last=False)
    in_progress.inc()
    decisions_total.labels("accepted", label).inc()
    launcher(record)
    return JSONResponse(status_code=202, content={**decision, "status": "accepted", "execution_id": record["id"]})


@app.get("/executions")
def executions(limit: int = 20):
    with _lock:
        items = list(_executions.values())[-max(1, min(limit, MAX_KEPT)):]
    return {"executions": list(reversed(items))}


@app.get("/executions/{execution_id}")
def execution(execution_id: str):
    with _lock:
        record = _executions.get(execution_id)
    if record is None:
        raise HTTPException(status_code=404, detail="unknown execution")
    return record


WATCHED = ("frontend", "api-gateway", "transaction-service", "database")


@app.get("/deployments")
def deployments():
    """Read-only rollout view of the application's deployments, for the dashboard (replicas, rollout in progress, ready)."""
    ns = settings()["namespace"]
    out = []
    for name in WATCHED:
        try:
            d = cluster.deployment(name, ns)
            settled = d["observed"] >= d["generation"] and d["updated"] == d["replicas"] and d["ready"] == d["replicas"] \
                and d["available"] == d["replicas"]
            out.append({"name": name, **d, "rolling_out": not settled})
        except Exception as exc:                                # noqa: BLE001
            out.append({"name": name, "error": str(exc)})
    return {"namespace": ns, "deployments": out}


@app.get("/state")
def state():
    now = time.monotonic()
    with _lock:
        return {"in_flight": [list(k) for k in _inflight],
                "cooldowns": [{"root_cause": k[0], "target_service": k[1], "remaining_s": round(v - now)}
                              for k, v in _cooldowns.items() if v > now]}
