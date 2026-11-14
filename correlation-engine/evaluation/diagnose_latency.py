"""Print what the cluster looked like while APILatencyHigh was stuck, from Prometheus's own history.

Run on the instance (kubectl must reach the cluster), for a window already in Prometheus:

    python3 diagnose_latency.py --day 2026-11-12 --from 18:05 --to 18:25

One row a minute. `alert` is whether APILatencyHigh was firing; `p95_5m` is the alert's own expression;
`p95_1m` is the same quantile over one minute, so it shows whether requests are slow NOW or only inside the
5-minute window; the per-service columns say which hop is slow; `slow%` is the share of gateway requests over
0.5 s; `throttle` is the fraction of CPU periods the autosre containers spent throttled by their limits;
`node_cpu` is the whole node. Stdlib only, read-only.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import urllib.parse
from datetime import datetime, timezone

PROM = "/api/v1/namespaces/monitoring/services/monitoring-kube-prometheus-prometheus:9090/proxy"
BUCKET = "http_request_duration_seconds_bucket"


def p95(service: str, window: str) -> str:
    return (f'histogram_quantile(0.95, sum by (le) (rate({BUCKET}{{service="{service}"}}[{window}])))')


COLUMNS = [
    ("alert", 'max(ALERTS{alertname="APILatencyHigh",alertstate="firing"}) or vector(0)'),
    ("p95_5m", p95("api-gateway", "5m")),
    ("p95_1m", p95("api-gateway", "1m")),
    ("txn_1m", p95("transaction-service", "1m")),
    ("db_1m", p95("database", "1m")),
    ("fe_1m", p95("frontend", "1m")),
    ("gw_rps", 'sum(rate(http_requests_total{service="api-gateway"}[1m]))'),
    ("slow%", f'100 * (1 - sum(rate({BUCKET}{{service="api-gateway",le="0.5"}}[1m])) '
              f'/ sum(rate({BUCKET}{{service="api-gateway",le="+Inf"}}[1m])))'),
    ("throttle", 'sum(rate(container_cpu_cfs_throttled_periods_total{namespace="autosre"}[1m])) '
                 '/ sum(rate(container_cpu_cfs_periods_total{namespace="autosre"}[1m]))'),
    ("node_cpu", '1 - avg(rate(node_cpu_seconds_total{mode="idle"}[1m]))'),
]


def query_range(expr: str, start: float, end: float, step: int) -> dict[int, float]:
    uri = (f"{PROM}/api/v1/query_range?query={urllib.parse.quote(expr)}"
           f"&start={start}&end={end}&step={step}")
    run = subprocess.run(["kubectl", "get", "--raw", uri], capture_output=True, text=True, timeout=120)
    if run.returncode != 0:
        raise SystemExit(run.stderr.strip())
    result = json.loads(run.stdout)["data"]["result"]
    if not result:
        return {}
    return {int(float(t)): float(v) for t, v in result[0]["values"]}


def epoch(day: str, hhmm: str) -> float:
    return datetime.strptime(f"{day} {hhmm}", "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).timestamp()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--day", required=True, help="UTC date, YYYY-MM-DD")
    ap.add_argument("--from", dest="start", required=True, help="UTC HH:MM")
    ap.add_argument("--to", dest="end", required=True, help="UTC HH:MM")
    ap.add_argument("--step", type=int, default=60)
    args = ap.parse_args()
    start, end = epoch(args.day, args.start), epoch(args.day, args.end)
    series = {name: query_range(expr, start, end, args.step) for name, expr in COLUMNS}
    print("time  " + " ".join(f"{name:>8}" for name, _ in COLUMNS))
    t = int(start)
    while t <= end:
        cells = []
        for name, _ in COLUMNS:
            v = series[name].get(t)
            cells.append(f"{'-':>8}" if v is None or v != v else f"{v:8.2f}")
        print(datetime.fromtimestamp(t, timezone.utc).strftime("%H:%M") + " " + " ".join(cells))
        t += args.step


if __name__ == "__main__":
    main()
