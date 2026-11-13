"""Test the live RCA pipeline against REAL failures of the lab's own resources.

Nothing here talks to a chaos endpoint or sends the engine an event. Each fault is an ordinary operating-system or
cluster action on a real resource, and the failure travels through the real path:

    resource -> cAdvisor / app metrics -> Prometheus -> alert rules -> Alertmanager -> webhook -> rule engine
             -> learned scorer -> second-opinion gate -> escalation / action

The engine is never told what was caused (`put_label(None)`); the ground truth lives only in this script's own trial
record. The engine, scorer and gate are left exactly as deployed (gate in shadow, ambiguity rule off): what the gate
and the rule *would* have done is read off the recorded alerts and scorer verdicts, and says so in the record.

Real faults (the mechanism, then what the pipeline can see):

  memory_pressure   a real process inside the transaction-service container holds memory until the container is ~93%
                    of its limit (cAdvisor sees it)                        known cause: high_memory_usage
  pool_exhaustion   the database's CPU limit is cut and a real client pod floods the frontend, so real requests pile
                    up in the transaction service's pool                  known cause: db_connection_pool_exhaustion
  db_outage         the database deployment is scaled to zero             no known cause: expect escalation
  pod_crash         PID 1 of the transaction-service container is killed repeatedly (a real crash loop)
                                                                          no known cause: expect escalation
  cpu_pressure      busy-loop processes inside the api-gateway container  no known cause: expect escalation
  network_partition a NetworkPolicy denies traffic into the database      no known cause: expect escalation
                    (only effective if the cluster's network plugin enforces policies; the run says if it did not)

Not testable end to end, and why (verified in the code, not assumed):
  disk              `db_disk_usage_ratio` is a fixed value (DISK_USAGE_RATIO) changed only through the chaos endpoint; a
                    real disk fill would never reach the pipeline.
  EC2 stop          the monitoring stack lives on the same instance; stopping it removes the thing being tested.

Trial = healthy check and baseline -> ONE real fault (or a healthy window, or two real faults for `pair`) -> the
pipeline reacts on its own -> record every webhook -> recover the resource -> verify it is healthy again.

    python3 real_aws_validate.py --out DIR --plan healthy:15,memory_pressure:3 [--hold 150] [--deadline-minutes N]

`--plan` is a comma list of name:count. `healthy:N` is N minutes with no fault; `pair:N` is N trials of memory_pressure
then pool_exhaustion once the engine has reacted to the first. Read the results with real_aws_report.py.
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import live_validate as lv                                   # noqa: E402  (cluster helpers only; no chaos calls)
from correlate import ambiguous_causes, correlate, firing_only  # noqa: E402
from signatures import SIGNATURES                            # noqa: E402

NS = lv.NS
HOG_MARK = "autosre-real-memory-hog"
BURN_MARK = "autosre-real-cpu-burn"
LOAD_POD = "real-burst"
NETPOL = "real-deny-database"
KNOWN = {"high_memory_usage", "db_connection_pool_exhaustion"}


# -- doing things to real resources -----------------------------------------------------------------------------

BACKGROUND = """\
# {mark}
import base64, os
if os.fork() == 0:
    os.setsid()
    fd = os.open(os.devnull, os.O_RDWR)
    for i in (0, 1, 2):
        os.dup2(fd, i)
    exec(base64.b64decode({payload!r}).decode())
"""

HOG = """\
import time
def rd(p):
    try:
        return open(p).read().strip()
    except OSError:
        return None
def usage():
    cur, mx = rd("/sys/fs/cgroup/memory.current"), rd("/sys/fs/cgroup/memory.max")
    if cur is None or mx in (None, "max"):
        cur = rd("/sys/fs/cgroup/memory/memory.usage_in_bytes")
        mx = rd("/sys/fs/cgroup/memory/memory.limit_in_bytes")
    return int(cur), int(mx)
held = []
while True:
    cur, mx = usage()
    if cur / mx >= {target}:
        break
    held.append(bytearray(b"x") * (1024 * 1024))
    time.sleep(0.05)
while True:
    time.sleep(3600)
"""

BURN = """\
while True:
    pass
"""

KILL_MARKED = """\
import os, signal
mark = {mark!r}
me = os.getpid()
for pid in os.listdir("/proc"):
    if pid.isdigit() and int(pid) != me:
        try:
            if mark.encode() in open("/proc/%s/cmdline" % pid, "rb").read():
                os.kill(int(pid), signal.SIGKILL)
        except OSError:
            pass
"""


def background_code(mark: str, body: str) -> str:
    payload = base64.b64encode(body.encode()).decode()
    return BACKGROUND.format(mark=mark, payload=payload)


def exec_python(deploy: str, code: str) -> str:
    return lv.kubectl("-n", NS, "exec", f"deploy/{deploy}", "--", "python", "-c", code, timeout=90)


def start_background(deploy: str, mark: str, body: str, copies: int = 1) -> None:
    for _ in range(copies):
        exec_python(deploy, background_code(mark, body))


def kill_marked(deploy: str, mark: str) -> None:
    try:
        exec_python(deploy, KILL_MARKED.format(mark=mark))
    except RuntimeError as exc:                         # the pod may already be gone
        lv.log(f"  kill_marked on {deploy}: {exc}")


def deploy_cpu_limit(deploy: str) -> str:
    out = lv.kubectl("-n", NS, "get", f"deploy/{deploy}", "-o",
                     "jsonpath={.spec.template.spec.containers[0].resources.limits.cpu}").strip()
    return out or "200m"


def rollout(*deploys: str) -> None:
    for d in deploys:
        lv.kubectl("-n", NS, "rollout", "status", f"deploy/{d}", "--timeout=240s", timeout=260)


# -- the faults -------------------------------------------------------------------------------------------------

@dataclass
class Fault:
    name: str
    resource: str
    cause: str | None                      # the engine's vocabulary; None = no known cause (correct outcome: escalate)
    start: Callable[[], None]
    stop: Callable[[], None]
    effect: Callable[[], float | None]     # a real measurement that shows the failure is actually happening
    effect_threshold: float
    effect_label: str
    tick: Callable[[float], None] | None = None
    state: dict | None = None


def _memory_start() -> None:
    start_background("transaction-service", HOG_MARK, HOG.format(target=0.93))


def _memory_stop() -> None:
    kill_marked("transaction-service", HOG_MARK)


def _memory_effect() -> float | None:
    return lv.memory_ratio()


def _pool_start(state: dict) -> None:
    state["db_cpu"] = deploy_cpu_limit("database")
    lv.kubectl("-n", NS, "set", "resources", "deploy/database", "--limits=cpu=10m")
    rollout("database")
    loop = "(while true; do curl -s -m 10 -o /dev/null http://frontend.autosre.svc.cluster.local:8000/; done) &"
    script = "i=0; while [ $i -lt 40 ]; do " + loop + " i=$((i+1)); done; wait"
    lv.kubectl("-n", NS, "run", LOAD_POD, "--restart=Never", "--image=curlimages/curl:8.11.1",
               "--image-pull-policy=IfNotPresent", "--labels=app=real-burst", "--", "sh", "-c", script)


def _pool_stop(state: dict) -> None:
    lv.kubectl("-n", NS, "delete", "pod", LOAD_POD, "--ignore-not-found", "--wait=false", check=False)
    lv.kubectl("-n", NS, "set", "resources", "deploy/database", f"--limits=cpu={state.get('db_cpu', '200m')}")
    rollout("database")


def _pool_effect() -> float | None:
    return lv.prom_scalar("max(db_connection_pool_active / db_connection_pool_max)", 0.0)


def _outage_start() -> None:
    lv.kubectl("-n", NS, "scale", "deploy/database", "--replicas=0")


def _outage_stop() -> None:
    lv.kubectl("-n", NS, "scale", "deploy/database", "--replicas=1")
    rollout("database")


def _outage_effect() -> float | None:
    available = lv.prom_scalar('sum(kube_deployment_status_replicas_available{namespace="autosre",deployment="database"})', 1.0)
    return 1.0 - (available or 0.0) if available is not None else None


def _crash_tick(elapsed: float, state: dict) -> None:
    # one real kill of PID 1 about every 50 s; the kubelet restarts the container each time
    due = int(elapsed // 50)
    if due > state.get("kills", 0):
        state["kills"] = due
        try:
            exec_python("transaction-service", "import os, signal; os.kill(1, signal.SIGKILL)")
        except RuntimeError:
            pass                                            # the connection drops as the container dies: expected


def _crash_effect() -> float | None:
    return lv.prom_scalar('sum(increase(kube_pod_container_status_restarts_total{namespace="autosre",'
                          'container="transaction-service"}[5m]))', 0.0)


def _cpu_start() -> None:
    start_background("api-gateway", BURN_MARK, BURN, copies=3)


def _cpu_stop() -> None:
    kill_marked("api-gateway", BURN_MARK)


def _cpu_effect() -> float | None:
    return lv.prom_scalar('histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket'
                          '{service="api-gateway"}[1m])))', 0.0)


NETPOL_YAML = f"""\
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: {NETPOL}
  namespace: {NS}
spec:
  podSelector:
    matchLabels:
      app: database
  policyTypes: [Ingress]
"""


def _net_start() -> None:
    lv.kubectl("apply", "-f", "-", stdin=NETPOL_YAML)


def _net_stop() -> None:
    lv.kubectl("-n", NS, "delete", "networkpolicy", NETPOL, "--ignore-not-found", check=False)


def _net_effect() -> float | None:
    return lv.prom_scalar('sum(rate(http_requests_total{service="transaction-service",status=~"5.."}[1m]))', 0.0)


def build_faults() -> dict[str, Fault]:
    pool_state: dict = {}
    crash_state: dict = {}
    return {
        "memory_pressure": Fault("memory_pressure", "transaction-service container", "high_memory_usage",
                                 _memory_start, _memory_stop, _memory_effect, 0.90, "memory working set / limit"),
        "pool_exhaustion": Fault("pool_exhaustion", "database CPU + client load on the transaction-service pool",
                                 "db_connection_pool_exhaustion", lambda: _pool_start(pool_state),
                                 lambda: _pool_stop(pool_state), _pool_effect, 0.90, "pool active / max", state=pool_state),
        "db_outage": Fault("db_outage", "database deployment", None, _outage_start, _outage_stop, _outage_effect,
                           1.0, "database replicas missing"),
        "pod_crash": Fault("pod_crash", "transaction-service container", None, lambda: crash_state.clear(),
                           lambda: None, _crash_effect, 3.0, "container restarts in 5 min",
                           tick=lambda e: _crash_tick(e, crash_state), state=crash_state),
        "cpu_pressure": Fault("cpu_pressure", "api-gateway container", None, _cpu_start, _cpu_stop, _cpu_effect,
                              1.0, "gateway p95 latency (s)"),
        "network_partition": Fault("network_partition", "database ingress (NetworkPolicy)", None, _net_start,
                                   _net_stop, _net_effect, 0.5, "transaction-service 5xx per second"),
    }


# -- recording what the pipeline did ---------------------------------------------------------------------------

def snapshot() -> dict:
    return {
        "at": lv.now_iso(),
        "alerts_firing": sorted(lv.firing()),
        "memory_ratio_txn": lv.memory_ratio(),
        "gateway_rps": lv.prom_scalar('sum(rate(http_requests_total{service="api-gateway"}[1m]))', 0.0),
        "gateway_p95_1m": lv.prom_scalar('histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket'
                                         '{service="api-gateway"}[1m])))'),
        "pool_ratio": lv.prom_scalar("max(db_connection_pool_active / db_connection_pool_max)"),
        "restarts_5m": lv.prom_scalar('sum(increase(kube_pod_container_status_restarts_total{namespace="autosre"}[5m]))'),
        "pods": lv.kubectl("-n", NS, "get", "pods", "--no-headers", "-o",
                           "custom-columns=N:.metadata.name,R:.status.containerStatuses[0].ready,"
                           "X:.status.containerStatuses[0].restartCount", check=False).strip().splitlines(),
    }


def parse_decisions(lines: list[str]) -> list[dict]:
    out = []
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        resp = rec.get("response") or {}
        alerts = firing_only(rec["payload"].get("alerts", []))
        named = resp.get("root_cause")
        opinion = resp.get("second_opinion") or {}
        ruled = correlate(alerts, True)
        out.append({
            "received_at": rec.get("received_at"),
            "alerts": sorted({a["labels"].get("alertname", "?") for a in alerts}),
            "status": resp.get("status"),
            "action": resp.get("action"),
            "rule_prediction": named or (resp.get("rule_root_cause") if resp.get("status") == "vetoed" else None),
            "rule_confidence": resp.get("confidence"),
            "playbook": SIGNATURES[named]["playbook"] if named in SIGNATURES else None,
            "escalated": named is None,
            "second_opinion": opinion.get("verdict"),
            "second_opinion_detail": {k: opinion.get(k) for k in ("scorer_service", "scorer_fault", "confidence", "reason")},
            "gate_would_withhold": opinion.get("verdict") == "disagree",
            "ambiguous_causes": ambiguous_causes(alerts),
            "ambiguity_rule_would": "escalate" if ruled is None else ruled["root_cause"],
        })
    return out


def probe_scorer(started: float, when: str) -> dict:
    """The scorer's own reading of the window: its service, fault and confidence, independent of any claim. Asked with
    a throwaway claim because /opinion returns the scorer's top choice whatever is claimed; it is not told the fault."""
    out = lv.ask_scorer("db_disk_full", "database", started + 60)
    return {"when": when, "scorer_service": out.get("scorer_service"), "scorer_fault": out.get("scorer_fault"),
            "confidence": out.get("confidence"), "verdict_on_throwaway_claim": out.get("verdict"),
            "ranking": out.get("ranking"), "reason": out.get("reason")}


def wait_recovered(timeout: int = 900) -> dict:
    """Seconds until no symptom alert is firing and traffic is flowing again."""
    began = time.time()
    clear = 0
    while time.time() - began < timeout:
        if not lv.firing() and (lv.prom_scalar('sum(rate(http_requests_total{service="api-gateway"}[1m]))', 0.0) or 0) > 1:
            clear += 1
            if clear >= 2:
                return {"recovered": True, "recovery_s": round(time.time() - began - 20, 1)}
        else:
            clear = 0
        time.sleep(20)
    return {"recovered": False, "recovery_s": None, "still_firing": sorted(lv.firing())}


def sample_effect(fault: Fault, peaks: dict) -> None:
    try:
        v = fault.effect()
    except Exception:                                          # noqa: BLE001 - a missed sample is not a failure
        return
    if v is not None and v > peaks.get(fault.name, float("-inf")):
        peaks[fault.name] = v


# -- trials ----------------------------------------------------------------------------------------------------

def healthy_baseline() -> bool:
    return lv.quiet_or_reset()


def run_fault_trial(index: int, faults: list[Fault], detect_timeout: int, hold: int, stagger: int = 60) -> dict:
    """One real fault, or two (the second started after the engine has reacted to the first plus `stagger` s)."""
    names = [f.name for f in faults]
    truth = [f.cause for f in faults if f.cause]
    lv.log(f"--- real trial {index}: {' + '.join(names)}")
    trial: dict = {"trial_id": index, "real_fault": " + ".join(names), "resource": "; ".join(f.resource for f in faults),
                   "faults": names, "truth": truth, "no_known_cause": not truth,
                   "mode": "pair" if len(faults) > 1 else "single", "contaminated": False}
    trial["baseline_healthy"] = healthy_baseline()
    trial["contaminated"] = not trial["baseline_healthy"]
    if trial["contaminated"]:
        trial["still_firing"] = list(lv.STILL_FIRING)
    trial["baseline"] = snapshot()
    before = len(lv.captured())
    lv.put_label(None)                                          # the engine is never told what is about to happen
    trial["started_at"] = lv.now_iso()
    started = time.time()
    peaks: dict = {}
    started_faults: list[Fault] = []
    try:
        faults[0].start()
        started_faults.append(faults[0])
        trial["fault_started_at"] = lv.now_iso()
        lv.log(f"  real fault started: {faults[0].name}")

        first_seen = None
        second_at = None
        probes = []
        held_until = None
        while True:
            time.sleep(10)
            elapsed = time.time() - started
            for f in started_faults:
                sample_effect(f, peaks)
                if f.tick:
                    f.tick(elapsed)
            now_count = len(lv.captured())
            if first_seen is None and now_count > before:
                first_seen = time.time()
                trial["detection_latency_s"] = round(first_seen - started, 1)
                lv.log(f"  engine received an incident after {trial['detection_latency_s']}s")
                probes.append(probe_scorer(started, "at first incident"))
            if first_seen and len(faults) > 1 and second_at is None and time.time() - first_seen >= stagger:
                faults[1].start()
                started_faults.append(faults[1])
                second_at = time.time()
                trial["second_fault_started_at"] = lv.now_iso()
                lv.log(f"  second real fault started: {faults[1].name}")
            if held_until is None and first_seen and (len(faults) == 1 or second_at):
                held_until = time.time() + hold
            if held_until and time.time() >= held_until:
                break
            if first_seen is None and elapsed > detect_timeout:
                lv.log("  no incident received before the timeout")
                break
        if first_seen:
            probes.append(probe_scorer(started, "end of hold"))
        trial["scorer_probes"] = probes
    finally:
        recover_from = time.time()
        trial["recover_started_at"] = lv.now_iso()
        for f in reversed(started_faults):
            try:
                f.stop()
            except Exception as exc:                            # noqa: BLE001 - keep recovering the rest
                lv.log(f"  stop {f.name} failed: {exc}")
                trial.setdefault("stop_errors", []).append(f"{f.name}: {exc}")
        trial.update(wait_recovered())
        if trial.get("recovery_s") is not None:
            trial["recovery_s"] = round(time.time() - recover_from - 20, 1)
        trial["after"] = snapshot()
    decisions = parse_decisions(lv.captured()[before:])
    trial["decisions"] = decisions
    trial["first_decision_s"] = None
    if decisions and decisions[0]["received_at"]:
        trial["first_decision_s"] = round(_seconds_between(trial["fault_started_at"], decisions[0]["received_at"]), 1)
    trial["effect_peaks"] = {f.name: {"peak": peaks.get(f.name), "threshold": f.effect_threshold, "measure": f.effect_label,
                                      "effective": peaks.get(f.name, float("-inf")) >= f.effect_threshold} for f in faults}
    trial["fault_effective"] = all(v["effective"] for v in trial["effect_peaks"].values())
    lv.log(f"  decisions: {[(d['rule_prediction'] or 'escalate', d['second_opinion']) for d in decisions]}; "
           f"effective={trial['fault_effective']}; recovered={trial.get('recovered')}")
    return trial


def _seconds_between(a: str, b: str) -> float:
    from datetime import datetime
    return (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds()


def run_healthy(index: int, minutes: int) -> dict:
    """No fault at all: does the pipeline stay quiet? Every incident, escalation or named cause here is a false one."""
    lv.log(f"--- healthy window {index}: {minutes} min, nothing is done to the system")
    trial: dict = {"trial_id": index, "real_fault": "none (healthy)", "resource": "whole stack", "faults": [], "truth": [],
                   "mode": "healthy", "contaminated": False, "baseline": snapshot()}
    before = len(lv.captured())
    lv.put_label(None)
    trial["started_at"] = lv.now_iso()
    end = time.time() + minutes * 60
    seen: set = set()
    samples = []
    while time.time() < end:
        time.sleep(30)
        firing_now = sorted(lv.firing())
        seen.update(firing_now)
        samples.append({"at": lv.now_iso(), "firing": firing_now})
    trial["alerts_ever_firing"] = sorted(seen)
    trial["firing_samples"] = samples
    trial["decisions"] = parse_decisions(lv.captured()[before:])
    trial["after"] = snapshot()
    lv.log(f"  healthy window: alerts seen {sorted(seen)}; incidents {len(trial['decisions'])}")
    return trial


# -- driver ----------------------------------------------------------------------------------------------------

def parse_plan(text: str) -> list[tuple[str, int]]:
    out = []
    for part in text.split(","):
        name, _, count = part.strip().partition(":")
        if not name:
            continue
        out.append((name, int(count or 1)))
    return out


def prepare_real() -> dict:
    """Bring the stack to a known state without touching the RCA configuration: chaos endpoints off (the manifest
    default; earlier harness runs left them on), the engine capture working, traffic flowing."""
    lv.log("restoring the manifests' default of no chaos endpoints, and checking the stack")
    for deploy in ("transaction-service", "api-gateway", "database"):
        lv.kubectl("-n", NS, "set", "env", f"deploy/{deploy}", "CHAOS_ENABLED-")
    rollout("transaction-service", "api-gateway", "database", "frontend", "correlation-engine", "second-opinion")
    lv.engine_selftest()
    deadline = time.time() + 600
    while time.time() < deadline:
        up = lv.prom_scalar('count(up{namespace="autosre"} == 1)', 0)
        rate = lv.prom_scalar("sum(rate(http_requests_total[1m]))", 0)
        if up >= 5 and rate > 1:
            break
        time.sleep(15)
    env = lv.kubectl("-n", NS, "get", "deploy/correlation-engine", "-o", "jsonpath={.spec.template.spec.containers[0].env}")
    return {"engine_env": env, "cpu_limits": {d: deploy_cpu_limit(d) for d in
                                              ("api-gateway", "transaction-service", "database", "frontend")},
            "started": lv.now_iso()}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--plan", required=True, help="e.g. healthy:15,memory_pressure:3,pair:3")
    ap.add_argument("--detect-timeout", type=int, default=420)
    ap.add_argument("--hold", type=int, default=150, help="seconds to keep the real fault after the engine reacts")
    ap.add_argument("--deadline-minutes", type=int, default=0)
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    faults = build_faults()
    plan = parse_plan(args.plan)
    unknown = [n for n, _ in plan if n not in faults and n not in ("healthy", "pair")]
    if unknown:
        raise SystemExit(f"unknown plan entries: {unknown}; faults are {sorted(faults)}, healthy, pair")

    conditions = prepare_real()
    conditions["plan"] = args.plan
    (args.out / "conditions.json").write_text(json.dumps(conditions, indent=1), encoding="utf-8")
    began = time.time()
    index = 0
    with open(args.out / "trials.jsonl", "w", encoding="utf-8") as fh:
        for name, count in plan:
            for _ in range(1 if name == "healthy" else count):
                if args.deadline_minutes and (time.time() - began) / 60 > args.deadline_minutes:
                    lv.log("deadline reached; stopping")
                    break
                index += 1
                try:
                    if name == "healthy":
                        trial = run_healthy(index, count)
                    elif name == "pair":
                        trial = run_fault_trial(index, [faults["memory_pressure"], faults["pool_exhaustion"]],
                                                args.detect_timeout, args.hold)
                    else:
                        trial = run_fault_trial(index, [faults[name]], args.detect_timeout, args.hold)
                except Exception as exc:                        # noqa: BLE001 - one bad trial is data
                    lv.log(f"  trial {index} errored: {exc}")
                    trial = {"trial_id": index, "real_fault": name, "error": str(exc), "contaminated": True}
                    for f in faults.values():
                        try:
                            f.stop()
                        except Exception:                       # noqa: BLE001
                            pass
                fh.write(json.dumps(trial) + "\n")
                fh.flush()
                (args.out / "incidents.jsonl").write_text("\n".join(lv.captured()) + "\n", encoding="utf-8")
    lv.log(f"done in {(time.time() - began) / 60:.0f} minutes; results in {args.out}")


if __name__ == "__main__":
    sys.exit(main())
