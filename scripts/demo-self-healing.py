"""One real, bounded Minikube pool-fault demonstration; no manual rollout restart.

Prerequisite: apply the reviewed base manifests and opt-in demo patches. This
script does not directly patch Deployments or post synthetic alerts.
"""
import argparse
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", default="minikube")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.context != "minikube":
        parser.error("This controlled demonstration is restricted to context minikube")
    args.out.mkdir(parents=True, exist_ok=False)
    base = ["kubectl", "--context=" + args.context, "--request-timeout=20s"]

    def kubectl(*command):
        return subprocess.check_output(base + list(command), text=True)

    def get(path):
        return json.loads(kubectl("get", "--raw", path))

    def service(name, path):
        port = 8001 if name == "remediation-dispatcher" else 8000
        return get(f"/api/v1/namespaces/autosre/services/{name}:{port}/proxy{path}")

    def query(expression):
        path = "/api/v1/namespaces/monitoring/services/monitoring-kube-prometheus-prometheus:9090/proxy"
        return get(path + "/api/v1/query?query=" + quote(expression, safe=""))["data"]["result"]

    # Explicit JSON transport from the dispatcher pod avoids kubectl raw POST's media type.
    def post(path, payload, target="transaction-service"):
        port = 8001 if target == "remediation-dispatcher" else 8000
        url = f"http://{target}.autosre.svc.cluster.local:{port}{path}"
        code = (
            "import json,urllib.request as u,urllib.error as e;"
            f"r=u.Request({url!r},data={json.dumps(payload)!r}.encode(),"
            "headers={'Content-Type':'application/json'},method='POST');"
            "reply=u.urlopen(r,timeout=20);print(json.dumps({'code':reply.status,'body':json.load(reply)}))"
        )
        return json.loads(kubectl("-n", "autosre", "exec", "deployment/remediation-dispatcher", "--", "python", "-B", "-c", code))

    def snapshot():
        items = get("/api/v1/namespaces/autosre/pods")["items"]
        dispatcher = next(pod["metadata"]["name"] for pod in items
                          if pod["metadata"].get("labels", {}).get("app") == "remediation-dispatcher"
                          and pod["status"]["phase"] == "Running")
        return {"pods": items,
                "deployment": get("/apis/apps/v1/namespaces/autosre/deployments/transaction-service"),
                "pool": service("transaction-service", "/recovery"),
                "metrics": query('remediation_success_total{namespace="autosre",pod="' + dispatcher + '"}'),
                "ml": query('second_opinion_verdicts_total{namespace="autosre",job="second-opinion",exported_service="transaction-service"}')}

    def save(name, data):
        (args.out / name).write_text(json.dumps(data, indent=2), encoding="utf-8")

    def require(condition, message):
        if not condition:
            raise RuntimeError(message)

    health = service("remediation-dispatcher", "/health")
    require(health["auto_remediate"] is True and health["cooldown_seconds"] == 600, "Controlled automatic execution is not armed")
    deployments = get("/apis/apps/v1/namespaces/autosre/deployments")["items"]
    envs = {d["metadata"]["name"]: {e["name"]: e.get("value") for e in d["spec"]["template"]["spec"]["containers"][0].get("env", [])} for d in deployments}
    require(envs["correlation-engine"]["SECOND_OPINION_MODE"] == "shadow", "ML must remain shadow")
    require(envs["transaction-service"]["CHAOS_ENABLED"] == "false", "Broad chaos must be disabled")
    require(envs["transaction-service"]["POOL_FAULT_ENABLED"] == "true", "Bounded pool hook not armed")
    active = query('ALERTS{namespace="autosre",alertstate="firing"}')
    # kube-prometheus-stack's self-inhibited bookkeeping alert is not an incident.
    require(not [a for a in active if a["metric"].get("alertname") != "InfoInhibitor"],
            "An existing AutoSRE incident would contaminate the demonstration")
    before = snapshot()
    require(all(pod["status"]["phase"] == "Running" and all(c["ready"] for c in pod["status"]["containerStatuses"])
                for pod in before["pods"]), "All AutoSRE pods must be Ready")
    require(service("second-opinion", "/ready")["status"] == "ready", "ML scorer not ready")
    require(service("correlation-engine", "/ready")["status"] == "ready", "Engine not ready")
    require(before["pool"]["pool_exhausted"] is False, "Pool must initially be healthy")
    save("before.json", before)
    start = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    seen, job_id, driver = {}, None, None
    outcome = {"started_at": start}
    try:
        injection = post("/chaos/pool", {"active_connections": before["pool"]["pool_max"]})
        require(injection["code"] == 200, "Pool injection rejected")
        save("injection.json", injection)
        # Genuine application failures generate ErrorRateHigh as well as the pool alert.
        # Stop once the restarted service actually serves a successful transaction.
        load = """import json,time,urllib.request as u,urllib.error as e
failed=0
for i in range(900):
    try:
        r=u.urlopen(u.Request('http://transaction-service.autosre.svc.cluster.local:8000/transaction',method='POST'),timeout=5)
        if failed and r.status==200:
            print(json.dumps({'failed_transactions':failed,'recovered_transaction_status':200}),flush=True)
            break
    except e.HTTPError as exc:
        if exc.code!=503: raise
        failed+=1
    time.sleep(0.3)
else: raise SystemExit('No recovered transaction before deadline')
"""
        driver = subprocess.Popen(base + ["-n", "autosre", "exec", "deployment/remediation-dispatcher", "--", "python", "-B", "-c", load], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        deadline = time.monotonic() + 420
        while time.monotonic() < deadline:
            alerts = query('ALERTS{namespace="autosre",alertstate="firing"}')
            for alert in alerts:
                seen[alert["metric"]["alertname"]] = alert
            text = kubectl("-n", "autosre", "logs", "deployment/remediation-dispatcher", "--since-time=" + start)
            matches = re.findall(r'"status": "accepted".*?"job_id": "([0-9a-f-]+)"', text)
            if matches:
                job_id = matches[-1]
                job = service("remediation-dispatcher", "/remediations/" + job_id)
                if job["status"] == "failed":
                    raise RuntimeError("Automatic remediation failed: " + json.dumps(job["details"]))
                if job["status"] == "succeeded":
                    save("remediation.json", job)
                    break
            print("Waiting for real alerts / automatic recovery; observed=" + ",".join(sorted(seen)), flush=True)
            time.sleep(10)
        else:
            raise RuntimeError("Automatic recovery timed out")
        require("DBPoolNearExhaustion" in seen and "ErrorRateHigh" in seen, "Missing genuine Prometheus firing alerts")
        out, err = driver.communicate(timeout=30)
        require(driver.returncode == 0, "Traffic driver failed: " + err)
        outcome["traffic"] = json.loads(out)
        after = snapshot()
        require(after["pool"]["pool_exhausted"] is False and after["pool"]["instance"] != before["pool"]["instance"], "Pool not recovered on new pod")
        require(after["deployment"]["spec"]["template"]["metadata"]["annotations"]["autosre.io/remediation-id"] == job_id, "Restart was not the dispatcher's operation")
        repeated = post("/dispatch", {"root_cause": "db_connection_pool_exhaustion", "service": "transaction-service", "confidence": 1.0}, "remediation-dispatcher")
        require(repeated["body"]["status"] == "cooldown", "Duplicate remediation was not blocked")
        time.sleep(5)
        require(snapshot()["deployment"]["metadata"]["generation"] == after["deployment"]["metadata"]["generation"], "Cooldown allowed another rollout")
        time.sleep(16)  # Let Prometheus scrape the completed outcome and cooldown.
        final = snapshot()
        require(sum(float(x["value"][1]) for x in final["metrics"]) >
                sum(float(x["value"][1]) for x in before["metrics"]), "Prometheus did not scrape remediation success")
        save("after-prometheus-scrape.json", final)
        outcome.update(status="succeeded", job_id=job_id, cooldown=repeated["body"], alerts_seen=seen)
        save("after-recovery.json", after)
    except Exception as exc:
        outcome.update(status="failed", reason=str(exc), job_id=job_id, alerts_seen=seen)
        raise
    finally:
        # Emergency application-state cleanup, never a manual restart or deletion.
        current = service("transaction-service", "/recovery")
        if current["pool_exhausted"]:
            outcome["emergency_pool_reset"] = post("/chaos/pool", {"active_connections": 2})
        if driver is not None and driver.poll() is None:
            # The traffic worker exits after observing recovery (including cleanup).
            try:
                driver.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                driver.terminate()
                driver.communicate(timeout=10)
        save("outcome.json", outcome)
        for name in ("correlation-engine", "second-opinion", "remediation-dispatcher"):
            (args.out / (name + ".log")).write_text(kubectl("-n", "autosre", "logs", "deployment/" + name, "--since-time=" + start), encoding="utf-8")
    print(json.dumps(outcome, indent=2), flush=True)


if __name__ == "__main__":
    main()
