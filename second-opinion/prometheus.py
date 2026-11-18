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
    # Kubernetes state, from kube-state-metrics (the same series the alert rules use). Each is 0 when healthy and rises when its fault is present, and each has an
    # `or` branch that yields 0 when the series is absent, so a baseline of "nothing wrong" exists for the fault window to be compared with.
    "down": (f'1 - clamp_max(max by (deployment) (kube_deployment_status_replicas_available{{namespace="{NAMESPACE}"}}), 1)', "deployment"),
    "unready": (f'max by (deployment) (kube_deployment_status_replicas_unavailable{{namespace="{NAMESPACE}"}})', "deployment"),
    "paused": (f'max by (deployment) (kube_deployment_spec_paused{{namespace="{NAMESPACE}"}})', "deployment"),
    "extra": (f'clamp_min(max by (deployment) (kube_deployment_spec_replicas{{namespace="{NAMESPACE}"}}) - 1, 0)', "deployment"),
    "restart": (f'sum by (container) (increase(kube_pod_container_status_restarts_total{{namespace="{NAMESPACE}",container!=""}}[2m]))', "container"),
    "svctype": (f'sum by (service) (kube_service_spec_type{{namespace="{NAMESPACE}",type=~"NodePort|LoadBalancer"}})'
                f' or (0 * sum by (service) (kube_service_spec_type{{namespace="{NAMESPACE}",type="ClusterIP"}}))', "service"),
    "noep": (f'1 - clamp_max(sum by (service) (label_replace(kube_endpointslice_endpoints{{namespace="{NAMESPACE}",ready="true"}}, "service", "$1", "endpointslice", "(.+)-[a-z0-9]{{5}}"))'
             f' or (0 * sum by (service) (label_replace(kube_endpointslice_info{{namespace="{NAMESPACE}"}}, "service", "$1", "endpointslice", "(.+)-[a-z0-9]{{5}}"))), 1)', "service"),
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
