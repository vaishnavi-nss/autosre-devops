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
from collections import Counter
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
# Aim just above the 0.9 alert threshold. The container is OOM-killed at 1.0, and the
# working set carries some jitter, so leave real headroom rather than aiming at the edge.
MEMORY_TARGET = 0.925


def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", flush=True)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# -- cluster access --------------------------------------------------------------------

def kubectl(*args: str, timeout: int = 180, check: bool = True, stdin: str | None = None) -> str:
    run = subprocess.run(["kubectl", *args], capture_output=True, text=True, timeout=timeout, input=stdin)
    if check and run.returncode != 0:
        raise RuntimeError(f"kubectl {' '.join(args)} failed: {run.stderr.strip()}")
    return run.stdout


def pod_http(deploy: str, method: str, path: str) -> str:
    """Call a service's HTTP port through the Kubernetes API server's service proxy.

    Deliberately NOT `kubectl exec`: that runs the client *inside the container's own
    memory cgroup*, so injecting memory pressure (which must sit just under the limit)
    made the injector's own overhead push the pod over it and get it OOM-killed - the
    exec'd process died with exit code 137. The proxy needs nothing inside the pod, and
    unlike a port-forward it survives the pod being replaced between trials.
    """
    uri = f"/api/v1/namespaces/{NS}/services/{deploy}:http/proxy{path}"
    if method == "GET":
        return kubectl("get", "--raw", uri, timeout=60)
    verb = {"POST": "create", "PUT": "replace"}[method]
    return kubectl(verb, "--raw", uri, "-f", "-", timeout=60, stdin="")


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


# -- second opinion ---------------------------------------------------------------------

# Where each fault really is, and a diagnosis that is deliberately wrong for it. A useful
# verifier should agree with the truth and disagree with the wrong one - the live trials
# alone cannot show the second half, because the rules were never wrong in them.
TRUTH = {
    "db_connection_pool_exhaustion": ("db_connection_pool_exhaustion", "transaction-service"),
    "high_memory_usage": ("high_memory_usage", "transaction-service"),
    "db_disk_full": ("db_disk_full", "database"),
    None: None,                                    # a gateway fault: no rule diagnosis is right
}
WRONG = {
    "db_connection_pool_exhaustion": ("db_disk_full", "database"),
    "high_memory_usage": ("db_disk_full", "database"),
    "db_disk_full": ("high_memory_usage", "transaction-service"),
    None: ("db_connection_pool_exhaustion", "transaction-service"),
}


def ask_scorer(cause: str, service: str, incident_start: float) -> dict:
    """Query the second-opinion service from inside the cluster; never raises."""
    return _scorer_post("opinion", {"root_cause": cause, "service": service, "incident_start": incident_start})


def ask_features(incident_start: float) -> dict:
    """The raw per-service scores for the incident window: training data for a model of our own."""
    return _scorer_post("features", {"incident_start": incident_start})


def _scorer_post(path: str, payload: dict) -> dict:
    body = json.dumps(payload)
    try:
        out = kubectl("-n", NS, "exec", "deploy/loadgen", "--", "curl", "-s", "-m", "20", "-X", "POST",
                      "-H", "content-type: application/json", "-d", body,
                      f"http://second-opinion.autosre.svc.cluster.local:8000/{path}", timeout=60)
        return json.loads(out)
    except Exception as exc:                       # noqa: BLE001 - recorded, not fatal
        return {"verdict": "unavailable", "reason": f"{type(exc).__name__}: {exc}"}


def probe_scorer(label: str | None, injected_at: float, when: str) -> dict:
    """Ask about the true diagnosis (if there is one) and a wrong one, for the live fault."""
    start = injected_at + 60                       # alerts fire 30-60s after the fault begins
    out = {"when": when}
    if TRUTH[label]:
        out["truth"] = ask_scorer(*TRUTH[label], start)
    out["wrong"] = ask_scorer(*WRONG[label], start)
    out["features"] = ask_features(start)
    return out


# -- environment -----------------------------------------------------------------------

def prepare() -> None:
    log("enabling fault injection and incident capture")
    for deploy in ("transaction-service", "api-gateway", "database"):
        kubectl("-n", NS, "set", "env", f"deploy/{deploy}", "CHAOS_ENABLED=true")
    kubectl("-n", NS, "set", "env", "deploy/correlation-engine", f"CAPTURE_PATH={CAPTURE_FILE}")
    for deploy in (*SERVICES, "correlation-engine", "second-opinion"):
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
    """Replace the pods. This discards every injected fault (they live in process memory).

    It does NOT make the alerts clear quickly, which this was first assumed to do. The
    rules use rate(...[5m]); a replaced pod's series goes stale, but its earlier samples
    stay inside the 5-minute range, so APILatencyHigh and ErrorRateHigh keep firing for
    up to ~5 minutes after a fault ends. Measured on a live cluster: about 8-9 minutes per
    trial, mostly spent in wait_quiet(). That recovery lag is a property of the alert
    design, not of this script. Rolling restarts keep a replica serving throughout, so
    the restart does not itself cause errors.
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
    mb = max(10, math.ceil((MEMORY_TARGET - baseline) * MEMORY_LIMIT_MIB))
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
            trial["probes"] = [probe_scorer(label, started, "at first incident")]
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
            extra = trial["params"]["held_mb"] + max(5, math.ceil((MEMORY_TARGET - memory_ratio()) * MEMORY_LIMIT_MIB))
            pod_http("transaction-service", "POST", f"/chaos/memory?mb={extra}")
            trial["params"]["topped_up_to_mb"] = extra
            topped_up = True

    if trial["first_incident_s"] is not None:
        trial["probes"].append(probe_scorer(label, started, "end of hold"))
        log(f"  scorer: {[(p['when'], p.get('truth', {}).get('verdict'), p['wrong']['verdict']) for p in trial['probes']]}")
    trial["firing_at_end"] = sorted(firing())
    trial["records"] = len(captured()) - before
    log(f"  held; {trial['records']} record(s); firing: {trial['firing_at_end']}")

    reset_cluster()
    trial["reset_at"] = now_iso()
    return trial


def run_collect_trial(index: int, label: str | None, rng: random.Random, settle: int, windows=(150, 270),
                      start_offsets=(60,)) -> dict:
    """Inject a fault and record what the metrics look like, without waiting for alerts.

    The full trial spends most of its ~9 minutes waiting for rate(...[5m]) alerts to clear. To train
    a scorer only the metric windows are needed, so this injects, samples the scorer's feature table
    at each of `windows` (seconds since injection), resets, and moves on: about 5 minutes a trial.
    """
    log(f"--- collect trial {index}: {label or 'gateway fault (unknown)'}")
    trial: dict = {"trial": index, "label": label, "contaminated": False, "mode": "collect"}
    trial["params"] = KINDS[label](rng)
    trial["injected_at"] = now_iso()
    started = time.time()
    trial["probes"] = []
    for seconds in windows:
        time.sleep(max(0.0, started + seconds - time.time()))
        # The scorer treats incident_start - 60 s as when the fault began. The harness's convention is
        # injection + 60 s (alerts fire ~60 s in), but the engine passes the earliest alert's startsAt,
        # which differs per alert type, so the scored window starts earlier or later than in training.
        # Several offsets record that spread (the default is the harness convention only).
        for offset in start_offsets:
            features = ask_features(started + offset)
            when = f"{seconds}s after injection" + ("" if offset == 60 else f", start+{offset}")
            trial["probes"].append({"when": when, "features": features})
            log(f"  {when}: {'scores recorded' if features.get('scores') else features.get('reason', features)}")
    reset_cluster()
    trial["reset_at"] = now_iso()
    time.sleep(settle)
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
    ap.add_argument("--collect", action="store_true",
                    help="record the scorer's feature tables only (no alerts, ~5 min a trial) to train "
                         "second-opinion/train_own.py")
    ap.add_argument("--settle", type=int, default=90, help="--collect: seconds to wait after each reset")
    ap.add_argument("--start-offsets", default="60",
                    help="--collect: seconds after injection to use as incident_start, comma-separated "
                         "(e.g. 30,60,90,120 to cover the spread of real alert startsAt values)")
    ap.add_argument("--windows", default="150,270",
                    help="--collect: seconds after injection at which to record the feature table. The engine "
                         "asks the scorer 50-110 s after injection, so include early ones (e.g. 75,90,105,120,150)")
    ap.add_argument("--resume", action="store_true",
                    help="continue a previous run in --out: keep its trials, redo errored ones, "
                         "and run only what is still missing")
    args = ap.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    trials_path = args.out / "trials.jsonl"
    rng = random.Random(args.seed)

    done: list[dict] = []
    if args.resume and trials_path.exists():
        done = [json.loads(line) for line in trials_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    have = Counter(t["label"] for t in done if "error" not in t and not t.get("contaminated"))
    first_index = max((t["trial"] for t in done), default=0) + 1

    plan: list[str | None] = []
    for label in KINDS:
        wanted = args.unknown if label is None else args.per_cause
        plan += [label] * max(0, wanted - have[label])
    # Different seed per resume so a redo does not replay identical random choices.
    random.Random(args.seed + first_index).shuffle(plan)
    log(f"plan: {len(plan)} trials to run ({args.per_cause} per cause + {args.unknown} unknown, "
        f"{len(done)} already recorded), seed {args.seed}")

    prepare()
    if done:
        log("resuming: replacing every pod so no earlier fault is still injected")
        reset_cluster()
    began = time.time()
    with open(trials_path, "a" if done else "w", encoding="utf-8") as trials_file:
        for i, label in enumerate(plan, first_index):
            if args.deadline_minutes and (time.time() - began) / 60 > args.deadline_minutes:
                log(f"deadline reached; stopping after {i - 1} trials")
                break
            try:
                trial = (run_collect_trial(i, label, rng, args.settle, tuple(int(w) for w in args.windows.split(',')),
                         tuple(int(o) for o in args.start_offsets.split(','))) if args.collect
                         else run_trial(i, label, rng, args.detect_timeout, args.hold))
            except Exception as exc:                      # keep going: one bad trial is data
                log(f"  trial {i} errored: {exc}")
                trial = {"trial": i, "label": label, "error": str(exc), "contaminated": True}
                try:
                    reset_cluster()
                except Exception as reset_exc:
                    log(f"  reset also failed: {reset_exc}")
            trials_file.write(json.dumps(trial) + "\n")
            trials_file.flush()
            if not args.collect:
                (args.out / "incidents.jsonl").write_text("\n".join(captured()) + "\n", encoding="utf-8")

    if not args.collect:
        (args.out / "incidents.jsonl").write_text("\n".join(captured()) + "\n", encoding="utf-8")
    log(f"done in {(time.time() - began) / 60:.0f} minutes; results in {args.out}")


if __name__ == "__main__":
    sys.exit(main())
