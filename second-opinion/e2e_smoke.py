"""End-to-end smoke test over real HTTP, no cluster needed.

fake Prometheus <- second-opinion service <- correlation engine <- webhook. Checks the paths the
in-process tests skip: both urllib clients, both uvicorn servers, a dead scorer. A memory fault in
transaction-service is staged in the fake metrics; the scorer should agree with the true diagnosis,
disagree with a wrong one (the engine vetoes it in gate mode), and a dead scorer must not stop the engine.

    python second-opinion/e2e_smoke.py
"""
import http.server
import json
import os
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import zlib
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]
PY = sys.executable
NOW = time.time()
CHAIN = ("frontend", "api-gateway", "transaction-service", "database")
# What the "cluster" is really suffering. A memory fault in the RCAEval data moves cpu, memory and
# latency together, so stage all three rather than memory alone.
FAULT_SERVICE = "transaction-service"
FAULT_FACTORS = {"cpu": 6.0, "mem": 8.0, "latency": 6.0}
INJECT = NOW - 150                                             # fault began 150s ago


def series(service, kind):
    rng = np.random.default_rng(zlib.crc32(f"{service}/{kind}".encode()))   # hash() is salted per process
    out = []
    t = int(NOW - 2400)
    while t <= NOW:
        v = 10 + rng.normal(0, 0.4)
        if service == FAULT_SERVICE and kind in FAULT_FACTORS and t >= INJECT:
            v *= FAULT_FACTORS[kind]
        out.append([t, f"{v:.4f}"])
        t += 15
    return out


class Prom(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)["query"][0]
        kind = ("mem" if "memory_working_set" in q else "cpu" if "cpu_usage" in q else
                "latency" if "duration_seconds_bucket" in q else "error" if "status=~" in q else "workload")
        label = "container" if kind in ("mem", "cpu") else "service"
        result = [{"metric": {label: s}, "values": series(s, kind)} for s in CHAIN]
        body = json.dumps({"status": "success", "data": {"resultType": "matrix", "result": result}}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(body)


def post(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"content-type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=30))


def wait(url):
    for _ in range(60):
        try:
            urllib.request.urlopen(url, timeout=1)
            return
        except Exception:
            time.sleep(0.5)
    raise SystemExit(f"never came up: {url}")


server = http.server.ThreadingHTTPServer(("127.0.0.1", 9099), Prom)
threading.Thread(target=server.serve_forever, daemon=True).start()

env = {**os.environ, "PROMETHEUS_URL": "http://127.0.0.1:9099", "MIN_CONFIDENCE": "0.0"}
so = subprocess.Popen([PY, "-m", "uvicorn", "app:app", "--port", "8101"], cwd=REPO / "second-opinion", env=env,
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
eng_env = {**env, "SECOND_OPINION_URL": "http://127.0.0.1:8101", "SECOND_OPINION_MODE": "gate"}
eng = subprocess.Popen([PY, "-m", "uvicorn", "app:app", "--port", "8100"], cwd=REPO / "correlation-engine",
                       env=eng_env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    wait("http://127.0.0.1:8101/ready")
    wait("http://127.0.0.1:8100/health")
    print("ready:", json.load(urllib.request.urlopen("http://127.0.0.1:8101/ready")))

    # 1) the scorer directly: a true claim and a wrong one, for a memory fault in transaction-service
    start = INJECT + 60
    truth = post("http://127.0.0.1:8101/opinion", {
        "root_cause": "high_memory_usage", "service": "transaction-service", "incident_start": start})
    wrong = post("http://127.0.0.1:8101/opinion", {
        "root_cause": "db_disk_full", "service": "database", "incident_start": start})
    print("truth :", {k: v for k, v in post("http://127.0.0.1:8101/opinion", {
        "root_cause": "high_memory_usage", "service": "transaction-service", "incident_start": start}).items() if k != "ranking"})
    print("wrong :", {k: v for k, v in post("http://127.0.0.1:8101/opinion", {
        "root_cause": "db_disk_full", "service": "database", "incident_start": start}).items() if k != "ranking"})

    # 2) the engine: a memory incident webhook whose alert start matches the fault
    fixture = json.loads((REPO / "correlation-engine" / "tests" / "fixtures" / "sample_alertmanager_payload.json").read_text())
    starts = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(INJECT + 60))
    memory_alerts = [
        {"status": "firing", "labels": {"alertname": "ContainerMemoryHigh", "service": "transaction-service", "namespace": "autosre"},
         "startsAt": starts},
        {"status": "firing", "labels": {"alertname": "APILatencyHigh", "service": "api-gateway", "namespace": "autosre"},
         "startsAt": starts}]
    body = {**fixture, "alerts": memory_alerts}
    reply = true_reply = post("http://127.0.0.1:8100/alerts", body)
    print("engine, true diagnosis:", {k: reply.get(k) for k in ("status", "root_cause", "service", "confidence")},
          "| opinion:", {k: reply["second_opinion"].get(k) for k in ("verdict", "scorer_service", "scorer_fault", "basis")})

    # 3) a diagnosis that contradicts reality: the engine believes the database has a full disk
    bad = [{"status": "firing", "labels": {"alertname": "DBDiskNearFull", "service": "database", "namespace": "autosre"},
            "startsAt": starts}]
    reply = wrong_reply = post("http://127.0.0.1:8100/alerts", {**fixture, "alerts": bad})
    print("engine, wrong diagnosis:", {k: reply.get(k) for k in ("status", "action", "rule_root_cause", "reason")})

    # 4) the scorer is down: the engine must carry on
    so.terminate(); so.wait(timeout=10)
    reply = post("http://127.0.0.1:8100/alerts", {**fixture, "alerts": memory_alerts})
    print("engine, scorer dead   :", {k: reply.get(k) for k in ("status", "root_cause")},
          "| opinion:", reply["second_opinion"]["verdict"])
    dead_reply = reply

    failures = []
    if truth["verdict"] != "agree":
        failures.append(f"true claim got {truth['verdict']}: {truth.get('reason')}")
    if wrong["verdict"] != "disagree":
        failures.append(f"wrong claim got {wrong['verdict']}")
    if true_reply["status"] != "correlated" or true_reply["second_opinion"]["verdict"] != "agree":
        failures.append("engine did not act on the true diagnosis in gate mode")
    if wrong_reply["status"] != "vetoed":
        failures.append("engine did not veto the wrong diagnosis in gate mode")
    if dead_reply["status"] != "correlated" or dead_reply["second_opinion"]["verdict"] != "unavailable":
        failures.append("a dead scorer stopped the engine")
    print(("FAILED: " + "; ".join(failures)) if failures else "PASSED: all five checks")
    sys.exit(1 if failures else 0)
finally:
    for p in (so, eng):
        if p.poll() is None:
            p.terminate()
    server.shutdown()
