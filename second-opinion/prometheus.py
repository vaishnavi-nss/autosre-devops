"""Fetch the per-service metrics the scorer needs from Prometheus.

The result is a frame with a `time` column and one `<service>_<kind>` column per series,
the same shape as RCAEval's metrics.parquet, so scoring.service_scores works on it unchanged.
Only the standard library is used for HTTP.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request

import pandas as pd

NAMESPACE = "autosre"
# kind -> (query, label that names the service). A single `[1m]` window keeps the series
# responsive; the 15s scrape interval gives it four samples.
QUERIES = {
    "cpu": (f'sum by (container) (rate(container_cpu_usage_seconds_total{{namespace="{NAMESPACE}",container!=""}}[1m]))',
            "container"),
    "mem": (f'sum by (container) (container_memory_working_set_bytes{{namespace="{NAMESPACE}",container!=""}})',
            "container"),
    "latency": ('histogram_quantile(0.95, sum by (service, le) (rate(http_request_duration_seconds_bucket[1m])))',
                "service"),
    "error": ('sum by (service) (rate(http_requests_total{status=~"5.."}[1m]))'
              ' / sum by (service) (rate(http_requests_total[1m]))', "service"),
    "workload": ('sum by (service) (rate(http_requests_total[1m]))', "service"),
    # Our own gauges; only a model trained on our data (train_own.py) asks for these.
    "disk": ("max by (service) (db_disk_usage_ratio)", "service"),
    "pool": ("max by (service) (db_connection_pool_active)", "service"),
}
DEFAULT_KINDS = ("cpu", "mem", "latency", "error", "workload")


def _get(base_url: str, query: str, start: float, end: float, step: int, timeout: float):
    params = urllib.parse.urlencode({"query": query, "start": start, "end": end, "step": step})
    with urllib.request.urlopen(f"{base_url.rstrip('/')}/api/v1/query_range?{params}", timeout=timeout) as response:
        body = json.load(response)
    if body.get("status") != "success":
        raise RuntimeError(f"prometheus error: {body.get('error', body)}")
    return body["data"]["result"]


def fetch_metrics(base_url: str, services, start: float, end: float, step: int = 15,
                  timeout: float = 5.0, kinds=DEFAULT_KINDS) -> pd.DataFrame:
    """Frame of metrics for `services` between start and end (epoch seconds)."""
    wanted = set(services)
    columns: dict = {}
    for kind in kinds:
        query, label = QUERIES[kind]
        for series in _get(base_url, query, start, end, step, timeout):
            service = series["metric"].get(label)
            if service not in wanted:
                continue
            values = pd.Series(
                {int(float(t)): float(v) for t, v in series["values"] if v not in ("NaN", "+Inf", "-Inf")})
            if not values.empty:
                columns[f"{service}_{kind}"] = values
    if not columns:
        return pd.DataFrame(columns=["time"])
    frame = pd.DataFrame(columns).sort_index()
    frame.index.name = "time"
    return frame.reset_index()
