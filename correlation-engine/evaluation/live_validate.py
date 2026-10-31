#!/usr/bin/env python3
"""Live validation (M4): inject real faults into the running cluster and record
what the real Prometheus -> Alertmanager -> correlation-engine pipeline delivers.

Unlike simulate.py, nothing here is assumed about how symptoms arrive: the alerts
are produced by the real rules, grouped by the real Alertmanager, and posted to
the real engine. What is still *staged* is the fault itself - it is injected
through the CHAOS_ENABLED endpoints, not caused by a real defect - so this
measures detection and correlation, not whether real failures look like the
injected ones.

Run on a machine with kubectl pointing at the cluster:

    python evaluation/live_validate.py --out results --per-cause 6 --unknown 4

Produces, in --out:
    incidents.jsonl   every incident the engine received, labelled with ground truth
    trials.jsonl      one line per trial: what was injected and when
Then:  python evaluation/live_report.py --results results

Stdlib only. All cluster access is through kubectl.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

NS = "autosre"
MON = "monitoring"
PROM = f"/api/v1/namespaces/{MON}/services/monitoring-kube-prometheus-prometheus:9090/proxy"
CAPTURE_FILE = "/tmp/incidents.jsonl"
SYMPTOMS = ("APILatencyHigh|ErrorRateHigh|DBPoolNearExhaustion|DBDiskNearFull|"
            "PodRestartsHigh|ContainerMemoryHigh")
SERVICES = ["transaction-service", "api-gateway", "database", "frontend"]
MEMORY_LIMIT_MIB = 128           # k8s/*.yaml


def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", flush=True)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# -- cluster access --------------------------------------------------------------------

def kubectl(*args: str, timeout: int = 180, check: bool = True) -> str:
    run = subprocess.run(["kubectl", *args], capture_output=True, text=True, timeout=timeout)
    if check and run.returncode != 0:
        raise RuntimeError(f"kubectl {' '.join(args)} failed: {run.stderr.strip()}")
    return run.stdout


def pod_http(deploy: str, method: str, path: str) -> str:
    """Call a service's own HTTP port from inside its pod.

    `kubectl exec` rather than a port-forward: a port-forward dies when the pod is
    replaced, which this script does between every trial.
    """
    code = (
        "import urllib.request as u;"
        f"r=u.urlopen(u.Request('http://127.0.0.1:8000{path}',method='{method}'),timeout=15);"
        "print(r.read().decode())"
    )
    return kubectl("-n", NS, "exec", f"deploy/{deploy}", "--", "python", "-c", code, timeout=60)


def prom(expr: str) -> list[dict]:
    raw = kubectl("get", "--raw", f"{PROM}/api/v1/query?query={urllib.parse.quote(expr)}", timeout=60)
    return json.loads(raw)["data"]["result"]


def prom_scalar(expr: str, default: float | None = None) -> float | None:
    result = prom(expr)
    return float(result[0]["value"][1]) if result else default


def firing() -> set[str]:
    rows = prom(f'ALERTS{{alertstate="firing", alertname=~"{SYMPTOMS}"}}')
    return {r["metric"]["alertname"] for r in rows}


def captured() -> list[str]:
    out = kubectl("-n", NS, "exec", "deploy/correlation-engine", "--", "sh", "-c",
                  f"cat {CAPTURE_FILE} 2>/dev/null || true", timeout=60)
    return [line for line in out.splitlines() if line.strip()]


def put_label(label: str | None) -> None:
    payload = json.dumps({"label": label})
    code = (
        "import urllib.request as u;"
        "r=u.Request('http://127.0.0.1:8000/capture/label',"
        f"data={payload!r}.encode(),method='PUT',"
        "headers={'content-type':'application/json'});"
        "print(u.urlopen(r,timeout=15).read().decode())"
    )
    kubectl("-n", NS, "exec", "deploy/correlation-engine", "--", "python", "-c", code, timeout=60)


# -- environment -----------------------------------------------------------------------

def prepare() -> None:
    log("enabling fault injection and incident capture")
    for deploy in ("transaction-service", "api-gateway", "database"):
        kubectl("-n", NS, "set", "env", f"deploy/{deploy}", "CHAOS_ENABLED=true")
    kubectl("-n", NS, "set", "env", "deploy/correlation-engine", f"CAPTURE_PATH={CAPTURE_FILE}")
    for deploy in (*SERVICES, "correlation-engine"):
        kubectl("-n", NS, "rollout", "status", f"deploy/{deploy}", "--timeout=240s", timeout=260)

    log("waiting for Prometheus to scrape every AutoSRE target")
    deadline = time.time() + 600
    while time.time() < deadline:
        up = prom_scalar('count(up{namespace="autosre"} == 1)', 0)
        rate = prom_scalar("sum(rate(http_requests_total[1m]))", 0)
        if up >= 5 and rate > 1:
            log(f"  {int(up)} targets up, {rate:.1f} req/s of traffic")
            return
        time.sleep(15)
    raise SystemExit("targets never came up or there is no traffic - is loadgen running?")


def wait_quiet(timeout: int = 600) -> bool:
    """Wait until no symptom alert is firing, so trials cannot bleed together."""
    deadline = time.time() + timeout
    clear = 0
    while time.time() < deadline:
        active = firing()
        clear = clear + 1 if not active else 0
        if clear >= 2:
            time.sleep(75)            # let Alertmanager flush the resolved notification
            return True
        log(f"  waiting for quiet; still firing: {sorted(active)}")
        time.sleep(20)
    return False


def reset_cluster() -> None:
    """Replace the pods. This discards injected state AND flushes the rate windows.

    The alert rules use rate(...[5m]); after a fault ends the old samples keep the
    ratio high for minutes. A replaced pod is a new time series, so the old one goes
    stale and the alert clears in seconds. Rolling restarts keep a replica serving
    throughout, so this does not itself cause errors.
    """
    kubectl("-n", NS, "rollout", "restart", *[f"deploy/{s}" for s in SERVICES])
    for service in SERVICES:
        kubectl("-n", NS, "rollout", "status", f"deploy/{service}", "--timeout=240s", timeout=260)


# -- fault injection --------------------------------------------------------------------

def inject_pool(rng: random.Random) -> dict:
    active = rng.choice([19, 20])        # 19: near-exhausted, 20: fully exhausted
    pod_http("transaction-service", "POST", f"/chaos/pool?active={active}")
    return {"active": active, "max": 20}


def inject_disk(rng: random.Random) -> dict:
    ratio = rng.choice([0.95, 0.99])     # 0.95: nearly full, 0.99: queries fail
    pod_http("database", "POST", f"/chaos/disk?ratio={ratio}")
    return {"ratio": ratio}


def memory_ratio() -> float:
    ratio = prom_scalar(
        'sum(container_memory_working_set_bytes{namespace="autosre",container="transaction-service"})'
        ' / sum(kube_pod_container_resource_limits{namespace="autosre",'
        'container="transaction-service",resource="memory"})')
    return 0.45 if ratio is None else ratio


def inject_memory(rng: random.Random) -> dict:
    """Hold enough memory to cross 90% of the limit, without being OOM-killed."""
    baseline = memory_ratio()
    mb = max(10, math.ceil((0.94 - baseline) * MEMORY_LIMIT_MIB))
    pod_http("transaction-service", "POST", f"/chaos/memory?mb={mb}")
    return {"baseline_ratio": round(baseline, 3), "held_mb": mb}


def inject_unknown(rng: random.Random) -> dict:
    """A fault no signature describes: gateway latency or errors, nothing deeper."""
    if rng.random() < 0.5:
        pod_http("api-gateway", "POST", "/chaos/latency?ms=900")
        return {"fault": "gateway latency 900ms"}
    pod_http("api-gateway", "POST", "/chaos/errors?fraction=0.4")
    return {"fault": "gateway errors 40%"}


KINDS = {
    "db_connection_pool_exhaustion": inject_pool,
    "db_disk_full": inject_disk,
    "high_memory_usage": inject_memory,
    None: inject_unknown,
}


# -- one trial --------------------------------------------------------------------------

def run_trial(index: int, label: str | None, rng: random.Random, detect_timeout: int, hold: int) -> dict:
    name = label or "unknown (expect escalate)"
    log(f"--- trial {index}: {name}")
    trial: dict = {"trial": index, "label": label, "contaminated": False}

    trial["contaminated"] = not wait_quiet()
    before = len(captured())
    put_label(label)
    trial["label_set_at"] = now_iso()

    trial["params"] = KINDS[label](rng)
    trial["injected_at"] = now_iso()
    started = time.time()
    log(f"  injected {trial['params']}")

    trial["first_incident_s"] = None
    while time.time() - started < detect_timeout:
        time.sleep(10)
        if len(captured()) > before:
            trial["first_incident_s"] = round(time.time() - started, 1)
            log(f"  engine received an incident after {trial['first_incident_s']}s")
            break
    else:
        log("  no incident received before the timeout")

    # Keep the fault in place so later symptoms fire and Alertmanager sends its
    # updated groups; for memory, top it up if the first allocation fell short.
    held_until = time.time() + (hold if trial["first_incident_s"] is not None else 30)
    topped_up = False
    while time.time() < held_until:
        time.sleep(15)
        if label == "high_memory_usage" and not topped_up and memory_ratio() < 0.9:
            extra = trial["params"]["held_mb"] + max(5, math.ceil((0.94 - memory_ratio()) * MEMORY_LIMIT_MIB))
            pod_http("transaction-service", "POST", f"/chaos/memory?mb={extra}")
            trial["params"]["topped_up_to_mb"] = extra
            topped_up = True

    trial["firing_at_end"] = sorted(firing())
    trial["records"] = len(captured()) - before
    log(f"  held; {trial['records']} record(s); firing: {trial['firing_at_end']}")

    reset_cluster()
    trial["reset_at"] = now_iso()
    return trial


# -- main -------------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=Path("results"))
    ap.add_argument("--per-cause", type=int, default=6, help="trials per known cause")
    ap.add_argument("--unknown", type=int, default=4, help="trials with no known cause")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--detect-timeout", type=int, default=420, help="seconds to wait for an incident")
    ap.add_argument("--hold", type=int, default=120, help="seconds to keep the fault after detection")
    ap.add_argument("--deadline-minutes", type=int, default=0,
                    help="stop starting new trials after this long (0 = no limit)")
    args = ap.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    rng = random.Random(args.seed)
    plan = [label for label in KINDS if label is not None for _ in range(args.per_cause)]
    plan += [None] * args.unknown
    rng.shuffle(plan)
    log(f"plan: {len(plan)} trials ({args.per_cause} per cause + {args.unknown} unknown), seed {args.seed}")

    prepare()
    began = time.time()
    trials_path = args.out / "trials.jsonl"
    with open(trials_path, "w", encoding="utf-8") as trials_file:
        for i, label in enumerate(plan, 1):
            if args.deadline_minutes and (time.time() - began) / 60 > args.deadline_minutes:
                log(f"deadline reached; stopping after {i - 1} trials")
                break
            try:
                trial = run_trial(i, label, rng, args.detect_timeout, args.hold)
            except Exception as exc:                      # keep going: one bad trial is data
                log(f"  trial {i} errored: {exc}")
                trial = {"trial": i, "label": label, "error": str(exc), "contaminated": True}
                try:
                    reset_cluster()
                except Exception as reset_exc:
                    log(f"  reset also failed: {reset_exc}")
            trials_file.write(json.dumps(trial) + "\n")
            trials_file.flush()
            (args.out / "incidents.jsonl").write_text("\n".join(captured()) + "\n", encoding="utf-8")

    (args.out / "incidents.jsonl").write_text("\n".join(captured()) + "\n", encoding="utf-8")
    log(f"done in {(time.time() - began) / 60:.0f} minutes; results in {args.out}")


if __name__ == "__main__":
    sys.exit(main())
