#!/usr/bin/env python3
"""Run the AutoSRE fault scenarios end to end against a real cluster and record the evidence.

    Fault -> Prometheus -> Alertmanager -> correlation engine -> ML second opinion -> dispatcher -> Ansible -> Kubernetes recovery

Run it on a machine whose kubectl points at the cluster (the AWS lab instance or a local Minikube), in the isolated `autosre`
namespace. Standard library only.

    python3 scripts/scenarios/run_scenarios.py --out results/run1                      # dry run: decisions only, nothing is fixed
    python3 scripts/scenarios/run_scenarios.py --out results/run1 --live               # live execution (needs approval)

Honesty rules this script follows:
  * Nothing is simulated by the script. Each fault is injected through the service's own chaos endpoint and everything after
    that is the real pipeline. Each scenario says what kind of fault it is: the pool fault sets the pool gauge (injected); the
    memory fault holds real memory; the disk fault writes real bytes into the database's bounded log directory.
  * A scenario `passed` only if Prometheus fired the expected alert, the engine named the expected cause with a recorded second
    opinion, the dispatcher ran the expected playbook to a verified success, AND an independent check afterwards confirms
    recovery. Anything else is reported as what it was (escalated by policy, dry run, not recovered, timed out).
  * `--live` switches on AUTO_REMEDIATE for the run and ALWAYS switches it off again, restores every environment override,
    resets the faults and scales back, even if the run fails.
"""
from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

NS, MON = "autosre", "monitoring"
PROM = f"/api/v1/namespaces/{MON}/services/monitoring-kube-prometheus-prometheus:9090/proxy"
AM = f"/api/v1/namespaces/{MON}/services/monitoring-kube-prometheus-alertmanager:9093/proxy"
DEPLOYMENTS = ("frontend", "api-gateway", "transaction-service", "database", "correlation-engine", "second-opinion", "remediation-dispatcher")
SYMPTOMS = "APILatencyHigh|ErrorRateHigh|DBPoolNearExhaustion|DBDiskNearFull|PodRestartsHigh|ContainerMemoryHigh"

SCENARIOS = {
    "pool": {"alert": "DBPoolNearExhaustion", "cause": "db_connection_pool_exhaustion", "playbook": "restart_service.yml",
             "service": "transaction-service", "kind": "injected: the pool gauge is set (no real connections are held)"},
    "memory": {"alert": "ContainerMemoryHigh", "cause": "high_memory_usage", "playbook": "scale_replicas.yml",
               "service": "transaction-service", "kind": "real: the container holds real memory"},
    "disk": {"alert": "DBDiskNearFull", "cause": "db_disk_full", "playbook": "clear_logs.yml",
             "service": "database", "kind": "real: real bytes are written to the database's bounded log directory"},
}
DEFAULT_ORDER = ["guardrails_dry", "pool", "cooldown", "memory", "disk", "unknown", "guardrails_live"]


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(t: datetime | None = None) -> str:
    return (t or now()).isoformat(timespec="seconds")


def log(msg: str) -> None:
    print(f"[{now():%H:%M:%S}] {msg}", flush=True)


class Cluster:
    """kubectl plus the service proxy, so nothing runs inside a pod's memory cgroup (see live_validate.pod_http)."""

    def kubectl(self, *args: str, timeout: int = 180, check: bool = True, stdin: str | None = None) -> str:
        run = subprocess.run(["kubectl", *args], capture_output=True, text=True, timeout=timeout, input=stdin)
        if check and run.returncode != 0:
            raise RuntimeError(f"kubectl {' '.join(args)} failed: {run.stderr.strip()}")
        return run.stdout

    def get(self, path: str, timeout: int = 60):
        raw = self.kubectl("get", "--raw", path, timeout=timeout)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw

    def post(self, path: str, body: dict | None = None, timeout: int = 90):
        raw = self.kubectl("create", "--raw", path, "-f", "-", timeout=timeout, stdin=json.dumps(body or {}))
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw

    def svc(self, name: str, path: str, port: str = "http") -> str:
        return f"/api/v1/namespaces/{NS}/services/{name}:{port}/proxy{path}"

    def prom(self, expr: str) -> list[dict]:
        return self.get(f"{PROM}/api/v1/query?query={urllib.parse.quote(expr)}")["data"]["result"]

    def prom_scalar(self, expr: str, default: float | None = None) -> float | None:
        rows = self.prom(expr)
        return float(rows[0]["value"][1]) if rows else default

    def firing(self, alert: str | None = None) -> list[dict]:
        name = alert or SYMPTOMS
        return self.prom(f'ALERTS{{alertstate="firing", namespace="{NS}", alertname=~"{name}"}}')

    def deployment(self, name: str) -> dict:
        return json.loads(self.kubectl("-n", NS, "get", "deploy", name, "-o", "json"))


def wait_for(fn, timeout: float, interval: float = 5, what: str = ""):
    """Poll until `fn()` returns something truthy. Returns (value, seconds_waited) or (None, seconds_waited) on timeout."""
    start = time.monotonic()
    while True:
        try:
            value = fn()
        except Exception as exc:                                # noqa: BLE001 - a transient API error is not a verdict
            log(f"  ({what}: {exc})")
            value = None
        if value:
            return value, round(time.monotonic() - start, 1)
        if time.monotonic() - start >= timeout:
            return None, round(time.monotonic() - start, 1)
        time.sleep(interval)


# ----------------------------------------------------------------------------------------------- pure helpers (unit tested)
def find_incident(incidents: list[dict], since: str, cause: str | None = None, escalated: bool | None = None) -> dict | None:
    """First incident received at or after `since` that matches. `incidents` is newest first, so scan oldest-first."""
    for inc in reversed(incidents):
        if inc.get("received_at", "") < since:
            continue
        if cause is not None and inc.get("root_cause") != cause:
            continue
        if escalated is not None and (inc.get("action") == "escalate_to_human") != escalated:
            continue
        return inc
    return None


def find_incident_with(incidents: list[dict], since: str, alertnames: set[str]) -> dict | None:
    """First incident at or after `since` whose alerts include every name in `alertnames` (the two-fault incident)."""
    for inc in reversed(incidents):
        if inc.get("received_at", "") >= since and alertnames <= {a.get("alertname") for a in inc.get("alerts", [])}:
            return inc
    return None


def find_execution(executions: list[dict], since: str, playbook: str) -> dict | None:
    for ex in reversed(executions):                              # newest first -> oldest first
        if ex.get("started_at", "") >= since and ex.get("playbook") == playbook:
            return ex
    return None


def classify(spec: dict, ev: dict) -> tuple[str, str]:
    """(outcome, why). `passed` needs the whole chain and an independent recovery check."""
    if not ev.get("alert_fired"):
        return "no_alert", f"{spec['alert']} did not fire"
    inc = ev.get("incident")
    if not inc:
        return "no_incident", "the engine received no incident with the expected cause"
    if not (inc.get("second_opinion") or {}).get("verdict"):
        return "no_verdict", "no second-opinion verdict was recorded"
    rem = inc.get("remediation") or {}
    if rem.get("status") in ("dry_run",):
        return "dry_run", "all checks passed but live execution is off, so nothing was fixed (not a recovery)"
    if rem.get("status") == "escalated":
        return "escalated_by_policy", f"the dispatcher escalated: {rem.get('reason')}"
    if rem.get("status") == "skipped":
        return "skipped", f"the dispatcher skipped: {rem.get('reason')}"
    ex = ev.get("execution")
    if not ex:
        return "no_execution", f"the dispatcher answered {rem.get('status')} but no execution was recorded"
    if ex.get("status") != "succeeded":
        return "execution_" + str(ex.get("status")), str(ex.get("reason"))
    if not ev.get("independent_recovery"):
        return "not_recovered", f"the dispatcher reported success but the independent check failed: {ev.get('recovery_detail')}"
    return "passed", "alert, diagnosis, second opinion, playbook and independent recovery all confirmed"


# ----------------------------------------------------------------------------------------------- the run
class Runner:
    def __init__(self, c: Cluster, out: Path, live: bool, cooldown: int, timeouts: dict):
        self.c, self.out, self.live, self.cooldown, self.t = c, out, live, cooldown, timeouts
        self.results: list[dict] = []
        self.environment: dict = {}

    # -- cluster state
    def set_env(self, deploy: str, **env: str) -> None:
        args = [f"{k}={v}" if v is not None else f"{k}-" for k, v in env.items()]
        self.c.kubectl("-n", NS, "set", "env", f"deploy/{deploy}", *args)

    def settle(self, *deploys: str) -> None:
        for d in deploys or DEPLOYMENTS:
            self.c.kubectl("-n", NS, "rollout", "status", f"deploy/{d}", "--timeout=240s", timeout=260)

    def engine_incidents(self) -> list[dict]:
        return self.c.get(self.c.svc("correlation-engine", "/incidents?limit=50")).get("incidents", [])

    def executions(self) -> list[dict]:
        return self.c.get(self.c.svc("remediation-dispatcher", "/executions?limit=50", "http")).get("executions", [])

    def dispatcher_state(self) -> dict:
        return self.c.get(self.c.svc("remediation-dispatcher", "/state"))

    def quiet(self, timeout: int = 600) -> bool:
        ok, _ = wait_for(lambda: not self.c.firing(), timeout, 15, "waiting for quiet")
        if ok:
            time.sleep(75)                                       # let Alertmanager flush the resolved notification
        return bool(ok)

    def reset_faults(self) -> None:
        for svc in ("transaction-service", "database"):
            try:
                self.c.post(self.c.svc(svc, "/chaos/reset"))
            except Exception as exc:                            # noqa: BLE001
                log(f"  reset {svc}: {exc}")

    # -- preflight and restore
    def preflight(self) -> dict:
        log("preflight: deployments, monitoring, scorer")
        self.settle()
        up = self.c.prom_scalar(f'count(up{{namespace="{NS}"}} == 1)', 0)
        am = self.c.get(f"{AM}/api/v2/status")
        health = self.c.get(self.c.svc("remediation-dispatcher", "/health"))
        engine = self.c.get(self.c.svc("correlation-engine", "/health"))
        scorer = self.c.get(self.c.svc("second-opinion", "/ready"))
        if up < 5:
            raise RuntimeError(f"Prometheus sees only {up} healthy autosre targets")
        if health.get("auto_remediate") is not False:
            raise RuntimeError("the dispatcher is not in its safe default (AUTO_REMEDIATE=false); refusing to start")
        pre = {"targets_up": up, "alertmanager": am.get("cluster", {}).get("status"), "dispatcher": health, "engine": engine, "scorer": scorer,
               "deployments": {d: self.c.deployment(d)["status"].get("readyReplicas") for d in DEPLOYMENTS}}
        self.environment["preflight"] = pre
        return pre

    def prepare(self) -> None:
        log("enabling fault injection and the run's dispatcher settings (they are restored at the end)")
        for d in ("transaction-service", "database"):
            self.set_env(d, CHAOS_ENABLED="true")
        self.set_env("remediation-dispatcher", COOLDOWN_SECONDS=str(self.cooldown), VERIFY_TIMEOUT_SECONDS="150")
        self.settle("transaction-service", "database", "remediation-dispatcher")
        if not self.quiet(self.t["quiet"]):
            raise RuntimeError("alerts are still firing before the first fault; stopping (failed preflight)")

    def go_live(self) -> None:
        log("LIVE: switching AUTO_REMEDIATE on for this run")
        self.set_env("remediation-dispatcher", AUTO_REMEDIATE="true")
        self.settle("remediation-dispatcher")
        health = self.c.get(self.c.svc("remediation-dispatcher", "/health"))
        if health.get("auto_remediate") is not True:
            raise RuntimeError("could not confirm AUTO_REMEDIATE=true")

    def restore(self) -> None:
        log("restoring: AUTO_REMEDIATE off, chaos off, faults reset, replicas back to 1")
        errors = []
        for step in (
            lambda: self.set_env("remediation-dispatcher", AUTO_REMEDIATE="false", COOLDOWN_SECONDS=None, VERIFY_TIMEOUT_SECONDS=None),
            lambda: self.reset_faults(),
            lambda: self.c.kubectl("-n", NS, "scale", "deploy/transaction-service", "--replicas=1"),
            lambda: self.set_env("transaction-service", CHAOS_ENABLED="false"),
            lambda: self.set_env("database", CHAOS_ENABLED="false"),
            lambda: self.set_env("correlation-engine", SECOND_OPINION_MODE="shadow", ESCALATE_ON_AMBIGUITY=None),
            lambda: self.settle(),
        ):
            try:
                step()
            except Exception as exc:                            # noqa: BLE001 - keep restoring the rest
                errors.append(str(exc))
        health = self.c.get(self.c.svc("remediation-dispatcher", "/health"))
        self.environment["restored"] = {"auto_remediate": health.get("auto_remediate"), "errors": errors}
        if health.get("auto_remediate") is not False:
            log("!!! AUTO_REMEDIATE is NOT confirmed off: switch it off by hand")

    # -- faults
    def inject(self, name: str) -> dict:
        if name == "pool":
            maximum = self.c.prom_scalar('max(db_connection_pool_max{service="transaction-service"})', 20) or 20
            n = math.ceil(maximum * 0.95)
            self.c.post(self.c.svc("transaction-service", "/chaos/pool"), {"active_connections": n})
            return {"active_connections": n, "pool_max": maximum}
        if name == "memory":
            limit_mib = int(self.c.deployment("transaction-service")["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]["memory"].rstrip("Mi"))
            held = 0
            for _ in range(4):
                ratio = self.memory_ratio()
                if ratio >= 0.92:
                    break
                mb = max(4, min(math.ceil((0.925 - ratio) * limit_mib), int(limit_mib * 0.5)))
                self.c.post(self.c.svc("transaction-service", "/chaos/memory"), {"megabytes": mb})
                held += mb
                time.sleep(20)
            return {"held_mb": held, "memory_ratio": self.memory_ratio(), "limit_mib": limit_mib}
        if name == "disk":
            out = self.c.post(self.c.svc("database", "/chaos/disk"), {"ratio": 0.95})
            return {"response": out, "disk": self.c.get(self.c.svc("database", "/disk"))}
        raise ValueError(name)

    def memory_ratio(self) -> float:
        r = self.c.prom_scalar('sum(container_memory_working_set_bytes{namespace="autosre",container="transaction-service"})'
                               ' / sum(kube_pod_container_resource_limits{namespace="autosre",container="transaction-service",resource="memory"})')
        return 0.45 if r is None else r

    # -- independent recovery checks (not the dispatcher's own word)
    def independent_recovery(self, name: str) -> tuple[bool, str]:
        spec = SCENARIOS[name]
        d = self.c.deployment(spec["service"])
        st, want = d["status"], d["spec"]["replicas"]
        healthy = st.get("readyReplicas", 0) == want and st.get("availableReplicas", 0) == want and st.get("updatedReplicas", 0) == want
        if not healthy:
            return False, f"{spec['service']} not fully ready: {st}"
        h = self.c.get(self.c.svc(spec["service"], "/health"))
        if h.get("status") != "ok":
            return False, f"/health answered {h}"
        if name == "memory":
            return (want == 3), f"{want} replicas ready (the leaking pod still holds its memory; the alert is expected to persist until it is replaced)"
        gone, waited = wait_for(lambda: not self.c.firing(spec["alert"]), self.t["resolve"], 10, f"{spec['alert']} to resolve")
        if not gone:
            return False, f"{spec['alert']} still firing after {waited}s"
        if name == "disk":
            disk = self.c.get(self.c.svc("database", "/disk"))
            return disk["ratio"] < 0.9, f"disk ratio {disk['ratio']}, log bytes {disk['log_bytes']}"
        pool = self.c.prom_scalar('max(db_connection_pool_active{service="transaction-service"})', 0)
        return True, f"{spec['alert']} resolved after {waited}s; pool active {pool}"

    # -- scenarios
    def run_fault(self, name: str) -> dict:
        spec = SCENARIOS[name]
        rec = {"scenario": name, "expected": {k: spec[k] for k in ("alert", "cause", "playbook", "service")}, "fault_kind": spec["kind"],
               "live": self.live, "timeline": {}, "evidence": {}}
        t0 = now(); since = iso(t0)
        log(f"=== {name}: injecting ({spec['kind']})")
        rec["timeline"]["injected_at"] = since
        rec["evidence"]["injection"] = self.inject(name)
        ev = {}
        alert, w = wait_for(lambda: self.c.firing(spec["alert"]), self.t["alert"], 5, f"{spec['alert']}")
        ev["alert_fired"] = bool(alert); ev["alert"] = alert[0]["metric"] if alert else None
        rec["timeline"]["alert_after_s"] = w
        if alert:
            am, _ = wait_for(lambda: [a for a in self.c.get(f"{AM}/api/v2/alerts") if a["labels"].get("alertname") == spec["alert"]],
                             self.t["engine"], 5, "Alertmanager")
            ev["alertmanager"] = bool(am)
            inc, w2 = wait_for(lambda: find_incident(self.engine_incidents(), since, cause=spec["cause"]), self.t["engine"], 5, "engine incident")
            ev["incident"] = inc
            rec["timeline"]["incident_after_s"] = round(w + w2, 1) if inc else None
            rem = (inc or {}).get("remediation") or {}
            if inc and rem.get("status") == "accepted":
                ex, w3 = wait_for(lambda: (lambda e: e if e and e.get("status") != "running" else None)(find_execution(self.executions(), since, spec["playbook"])),
                                  self.t["execute"], 10, "execution")
                ev["execution"] = ex or find_execution(self.executions(), since, spec["playbook"])
                rec["timeline"]["execution_s"] = ev["execution"].get("duration_s") if ev.get("execution") else None
                if ex and ex.get("status") == "succeeded":
                    ev["independent_recovery"], ev["recovery_detail"] = self.independent_recovery(name)
        rec["evidence"].update(ev)
        rec["outcome"], rec["why"] = classify(spec, ev)
        rec["passed"] = rec["outcome"] == "passed"
        log(f"    -> {rec['outcome']}: {rec['why']}")
        self.reset_faults()
        if name == "memory":
            self.c.kubectl("-n", NS, "scale", "deploy/transaction-service", "--replicas=1")
        self.settle("transaction-service", "database")
        if not self.quiet(self.t["quiet"]):
            rec["warning"] = "alerts had not cleared by the time the next scenario was due"
        return rec

    def run_unknown(self) -> dict:
        since = iso()
        n_before = len(self.executions())
        rec = {"scenario": "unknown", "live": self.live, "fault_kind": "injected: errors on the transaction service (no signature fits)",
               "timeline": {"injected_at": since}, "evidence": {}}
        log("=== unknown: error rate with no deciding symptom")
        self.c.post(self.c.svc("transaction-service", "/chaos/errors"), {"rate": 0.5})
        alert, _ = wait_for(lambda: self.c.firing("ErrorRateHigh"), self.t["alert"], 5, "ErrorRateHigh")
        inc, _ = wait_for(lambda: find_incident(self.engine_incidents(), since, escalated=True), self.t["engine"], 5, "escalated incident") if alert else (None, 0)
        time.sleep(30)
        n_after = len(self.executions())
        rec["evidence"] = {"alert_fired": bool(alert), "incident": inc, "executions_before": n_before, "executions_after": n_after}
        ok = bool(alert and inc and inc.get("root_cause") is None and n_after == n_before)
        rec["outcome"], rec["passed"] = ("passed" if ok else "failed"), ok
        rec["why"] = "escalated to a human; nothing was executed" if ok else "expected an escalation with no execution"
        log(f"    -> {rec['outcome']}: {rec['why']}")
        self.reset_faults(); self.settle("transaction-service")
        self.quiet(self.t["quiet"])
        return rec

    def run_cooldown(self) -> dict:
        since = iso(); n_before = len(self.executions())
        rec = {"scenario": "cooldown", "live": self.live, "fault_kind": SCENARIOS["pool"]["kind"], "timeline": {"injected_at": since}, "evidence": {}}
        if not self.live:
            rec.update(outcome="not_run", passed=False, why="cooldown is only meaningful after a live execution"); return rec
        log("=== cooldown: the same fault again, inside the cooldown")
        self.inject("pool")
        alert, _ = wait_for(lambda: self.c.firing("DBPoolNearExhaustion"), self.t["alert"], 5, "alert")
        inc, _ = wait_for(lambda: find_incident(self.engine_incidents(), since, cause="db_connection_pool_exhaustion"), self.t["engine"], 5, "incident") if alert else (None, 0)
        rem = (inc or {}).get("remediation") or {}
        time.sleep(20)
        n_after = len(self.executions())
        rec["evidence"] = {"alert_fired": bool(alert), "incident": inc, "dispatcher_state": self.dispatcher_state(), "executions_before": n_before, "executions_after": n_after}
        ok = bool(inc) and rem.get("status") == "skipped" and rem.get("reason") == "cooldown" and n_after == n_before
        rec["outcome"], rec["passed"] = ("passed" if ok else "failed"), ok
        rec["why"] = f"dispatcher answered {rem.get('status')}/{rem.get('reason')}; executions {n_before} -> {n_after}"
        log(f"    -> {rec['outcome']}: {rec['why']}")
        self.reset_faults(); self.settle("transaction-service")
        self.quiet(self.t["quiet"])
        return rec

    def run_guardrails(self, live: bool) -> dict:
        """Direct checks of the dispatcher's policy with hand-made payloads. These are API checks, not injected faults."""
        good = {"root_cause": "db_connection_pool_exhaustion", "confidence": 0.9, "service": "transaction-service", "second_opinion": {"verdict": "agree"}}
        cases = {
            "low_confidence": ({**good, "confidence": 0.6}, "escalated"),
            "unknown_cause": ({**good, "root_cause": "cosmic_rays"}, "escalated"),
            "second_opinion_disagrees": ({**good, "second_opinion": {"verdict": "disagree"}}, "escalated"),
            "second_opinion_missing": ({k: v for k, v in good.items() if k != "second_opinion"}, "escalated"),
            "wrong_service": ({**good, "service": "database"}, "escalated"),
            "malformed_confidence": ({**good, "confidence": "high"}, "escalated"),
        }
        if not live:
            cases["valid_but_execution_off"] = (good, "dry_run")
        rec = {"scenario": "guardrails_live" if live else "guardrails_dry", "live": live, "fault_kind": "direct API checks of the dispatcher policy (no fault injected)",
               "timeline": {"at": iso()}, "evidence": {}}
        n_before = len(self.executions())
        for name, (payload, want) in cases.items():
            got = self.c.post(self.c.svc("remediation-dispatcher", "/dispatch"), payload)
            rec["evidence"][name] = {"expected": want, "got": got.get("status"), "reason": got.get("reason") or got.get("message"), "ok": got.get("status") == want}
        n_after = len(self.executions())
        ok = all(v["ok"] for v in rec["evidence"].values()) and n_after == n_before
        rec["evidence"]["executions"] = {"before": n_before, "after": n_after}
        rec["outcome"], rec["passed"] = ("passed" if ok else "failed"), ok
        rec["why"] = f"{sum(v['ok'] for k, v in rec['evidence'].items() if k != 'executions')}/{len(cases)} guardrail checks held; no execution was started"
        log(f"=== {rec['scenario']}: {rec['outcome']}: {rec['why']}")
        return rec

    def run_pair(self, kind: str) -> dict:
        """Two real faults at once (memory, then the pool) on the transaction service, with the engine's ambiguity rule or the
        second-opinion gate switched on. Decisions only: live execution is forced OFF here, because acting on one of two faults
        is exactly what these scenarios examine."""
        env = {"ESCALATE_ON_AMBIGUITY": "true"} if kind == "ambiguity" else {"SECOND_OPINION_MODE": "gate"}
        rec = {"scenario": f"pair_{kind}", "live": False, "fault_kind": "memory (real) then pool (injected) on transaction-service; engine setting " + json.dumps(env),
               "timeline": {}, "evidence": {}}
        self.set_env("remediation-dispatcher", AUTO_REMEDIATE="false")
        self.set_env("correlation-engine", **env)
        self.settle("remediation-dispatcher", "correlation-engine")
        since = iso(); n_before = len(self.executions())
        rec["timeline"]["injected_at"] = since
        log(f"=== pair_{kind}: memory then pool, decisions only")
        rec["evidence"]["memory"] = self.inject("memory")
        wait_for(lambda: self.c.firing("ContainerMemoryHigh"), self.t["alert"], 5, "memory alert")
        rec["evidence"]["pool"] = self.inject("pool")
        both = lambda: find_incident_with(self.engine_incidents(), since, {"DBPoolNearExhaustion", "ContainerMemoryHigh"})
        inc, _ = wait_for(both, self.t["engine"] + 120, 10, "an incident carrying both alerts")
        everything = [i for i in self.engine_incidents() if i.get("received_at", "") >= since]
        rec["evidence"].update(incident=inc, incidents_since_injection=everything, executions_before=n_before, executions_after=len(self.executions()))
        if kind == "ambiguity":
            ok = bool(inc) and inc.get("action") == "escalate_to_human" and "deciding symptoms" in (inc.get("reason") or "")
            rec["why"] = "escalated because two causes' deciding symptoms fired together" if ok else "expected an ambiguity escalation"
        else:
            ok = bool(inc)
            rec["why"] = f"engine status for the two-fault incident: {(inc or {}).get('status')}; second opinion {((inc or {}).get('second_opinion') or {}).get('verdict')} (recorded, not asserted)"
        ok = ok and rec["evidence"]["executions_after"] == n_before
        rec["outcome"], rec["passed"] = ("passed" if ok else "failed"), ok
        log(f"    -> {rec['outcome']}: {rec['why']}")
        self.reset_faults(); self.c.kubectl("-n", NS, "scale", "deploy/transaction-service", "--replicas=1")
        self.set_env("correlation-engine", **{k: None for k in env}) if kind == "ambiguity" else self.set_env("correlation-engine", SECOND_OPINION_MODE="shadow")
        self.settle("transaction-service", "correlation-engine")
        self.quiet(self.t["quiet"])
        return rec

    def write(self) -> None:
        self.out.mkdir(parents=True, exist_ok=True)
        (self.out / "scenarios.json").write_text(json.dumps({"finished_at": iso(), "live": self.live, "environment": self.environment,
                                                             "results": self.results}, indent=1, default=str), encoding="utf-8")
        (self.out / "report.md").write_text(report(self.results, self.live, self.environment), encoding="utf-8")


def report(results: list[dict], live: bool, env: dict) -> str:
    lines = ["# AutoSRE scenario run", "", f"Mode: **{'LIVE execution' if live else 'dry run (decisions only, nothing fixed)'}**.", "",
             "| Scenario | Fault | Outcome | Alert after | Incident after | Cause | ML verdict | Playbook run | Recovered | Why |",
             "|---|---|---|---:|---:|---|---|---|---|---|"]
    for r in results:
        ev, tl = r.get("evidence", {}), r.get("timeline", {})
        inc = ev.get("incident") or {}
        ex = ev.get("execution") or {}
        lines.append("| {} | {} | **{}** | {} | {} | {} | {} | {} | {} | {} |".format(
            r["scenario"], (r.get("fault_kind") or "").split(":")[0], r.get("outcome"), tl.get("alert_after_s", "-"), tl.get("incident_after_s", "-"),
            inc.get("root_cause", "-"), (inc.get("second_opinion") or {}).get("verdict", "-"), ex.get("playbook", "-"),
            {True: "yes", False: "NO", None: "-"}[ev.get("independent_recovery")], r.get("why", "")))
    lines += ["", f"Passed: {sum(1 for r in results if r.get('passed'))} of {len(results)}.",
              f"Restore: {json.dumps(env.get('restored'))}", "",
              "Pool exhaustion injects the pool gauge (no real connections); memory and disk hold real memory and real bytes. A memory scale-out does not remove "
              "the leak in the original pod, so its alert is expected to persist; recovery there means the extra capacity is ready and the service is healthy."]
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True)
    ap.add_argument("--scenarios", default=",".join(DEFAULT_ORDER), help=f"comma list from: {', '.join(DEFAULT_ORDER)}, pair_ambiguity, pair_gate")
    ap.add_argument("--live", action="store_true", help="turn AUTO_REMEDIATE on for this run (restored afterwards). Needs approval.")
    ap.add_argument("--cooldown", type=int, default=900, help="dispatcher cooldown (s) during the run")
    ap.add_argument("--timeout-alert", type=int, default=240)
    ap.add_argument("--timeout-engine", type=int, default=240)
    ap.add_argument("--timeout-execute", type=int, default=420)
    ap.add_argument("--timeout-resolve", type=int, default=300)
    ap.add_argument("--timeout-quiet", type=int, default=900)
    a = ap.parse_args(argv)
    timeouts = {"alert": a.timeout_alert, "engine": a.timeout_engine, "execute": a.timeout_execute, "resolve": a.timeout_resolve, "quiet": a.timeout_quiet}
    runner = Runner(Cluster(), Path(a.out), a.live, a.cooldown, timeouts)
    wanted = [s.strip() for s in a.scenarios.split(",") if s.strip()]
    if not a.live:
        wanted = [s for s in wanted if s not in ("cooldown", "guardrails_live")]
    exit_code = 0
    try:
        runner.preflight()
        runner.prepare()
        went_live = False
        for s in wanted:
            if a.live and not went_live and s not in ("guardrails_dry",):
                runner.go_live(); went_live = True
            if s == "guardrails_dry":
                runner.results.append(runner.run_guardrails(live=False))
            elif s == "guardrails_live":
                runner.results.append(runner.run_guardrails(live=True))
            elif s == "unknown":
                runner.results.append(runner.run_unknown())
            elif s == "cooldown":
                runner.results.append(runner.run_cooldown())
            elif s in ("pair_ambiguity", "pair_gate"):
                runner.results.append(runner.run_pair(s.split("_", 1)[1]))
            elif s in SCENARIOS:
                runner.results.append(runner.run_fault(s))
            else:
                log(f"unknown scenario {s!r}; skipped")
            runner.write()
    except Exception as exc:                                    # noqa: BLE001 - stop on a failed preflight or anything unexpected
        log(f"STOPPED: {exc}")
        runner.environment["stopped"] = str(exc)
        exit_code = 2
    finally:
        runner.restore()
        runner.write()
    failed = [r["scenario"] for r in runner.results if not r.get("passed")]
    log(f"done: {len(runner.results) - len(failed)} passed, failed or not recovered: {failed or 'none'}")
    return exit_code or (1 if failed else 0)


if __name__ == "__main__":
    sys.exit(main())
