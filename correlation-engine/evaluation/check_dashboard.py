#!/usr/bin/env python3
"""Check the AutoSRE Grafana dashboard against a live cluster (M7 verification).

For each panel query in monitoring/grafana-dashboards/autosre.json:
  - run it against the real Prometheus and report how many series it returns;
and confirm Grafana itself has imported the dashboard.

A query that errors is a broken panel. A query that returns nothing is not
necessarily broken - e.g. the confidence panel is empty until the engine has made
a diagnosis - so empty results are reported, not failed, and the run's exit status
only reflects errors and a missing dashboard.

    python evaluation/check_dashboard.py

Needs kubectl pointing at the cluster. Stdlib only.
"""

from __future__ import annotations

import json
import subprocess
import sys
import urllib.parse
from pathlib import Path

DASHBOARD = Path(__file__).resolve().parents[2] / "monitoring" / "grafana-dashboards" / "autosre.json"
PROM = "/api/v1/namespaces/monitoring/services/monitoring-kube-prometheus-prometheus:9090/proxy"
GRAFANA = "/api/v1/namespaces/monitoring/services/monitoring-grafana:80/proxy"


def get_raw(path: str) -> dict:
    run = subprocess.run(["kubectl", "get", "--raw", path], capture_output=True, text=True, timeout=60)
    if run.returncode != 0:
        raise RuntimeError(run.stderr.strip() or "kubectl failed")
    return json.loads(run.stdout)


def main() -> int:
    dashboard = json.loads(DASHBOARD.read_text(encoding="utf-8"))
    problems = 0

    print(f"Dashboard '{dashboard['title']}': checking every panel query against Prometheus\n")
    print(f"{'panel':<52} {'series':>6}  result")
    print("-" * 78)
    for panel in dashboard["panels"]:
        for target in panel.get("targets", []):
            title = panel["title"][:50]
            try:
                reply = get_raw(f"{PROM}/api/v1/query?query={urllib.parse.quote(target['expr'])}")
                if reply.get("status") != "success":
                    raise RuntimeError(reply.get("error", "query failed"))
                series = reply["data"]["result"]
                note = "ok" if series else "empty (no data yet)"
                print(f"{title:<52} {len(series):>6}  {note}")
            except Exception as exc:                      # a broken query is a broken panel
                problems += 1
                print(f"{title:<52} {'-':>6}  ERROR: {exc}")

    print("\nGrafana import:")
    try:
        found = get_raw(f"{GRAFANA}/api/dashboards/uid/{dashboard['uid']}")
        panels = [p for p in found["dashboard"].get("panels", []) if p["type"] != "row"]
        print(f"  imported as '{found['dashboard']['title']}' with {len(panels)} panels")
    except Exception as exc:
        problems += 1
        print(f"  NOT imported: {exc}")

    print(f"\n{'OK' if not problems else str(problems) + ' problem(s)'}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
