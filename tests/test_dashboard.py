"""The Grafana dashboard stays valid and every remediation panel charts a metric the dispatcher really exports."""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DASH = json.loads((ROOT / "monitoring" / "grafana-dashboards" / "autosre.json").read_text(encoding="utf-8"))


def test_ids_are_unique_and_panels_do_not_overlap():
    ids = [p["id"] for p in DASH["panels"]]
    assert len(ids) == len(set(ids))
    boxes = [(p["gridPos"]["x"], p["gridPos"]["y"], p["gridPos"]["w"], p["gridPos"]["h"], p["title"]) for p in DASH["panels"] if p["type"] != "row"]
    for i, (x1, y1, w1, h1, t1) in enumerate(boxes):
        for x2, y2, w2, h2, t2 in boxes[i + 1:]:
            apart = x1 + w1 <= x2 or x2 + w2 <= x1 or y1 + h1 <= y2 or y2 + h2 <= y1
            assert apart, f"{t1!r} overlaps {t2!r}"


def test_remediation_panels_use_metrics_the_dispatcher_exports():
    source = (ROOT / "remediation-dispatcher" / "app.py").read_text(encoding="utf-8")
    exported = set(re.findall(r'(?:Counter|Gauge|Histogram)\(\s*"(remediation_[a-z_]+)"', source))
    assert {"remediation_decisions_total", "remediation_executions_total", "remediation_skipped_total", "remediation_in_progress",
            "remediation_last_success_timestamp"} <= exported
    used = set()
    for p in DASH["panels"]:
        for t in p.get("targets", []):
            used |= set(re.findall(r"remediation_[a-z_]+", t["expr"]))
    assert used and used <= exported, used - exported
    titles = {p["title"] for p in DASH["panels"]}
    assert {"Dispatch decisions by outcome", "Executions by result", "Not run, and why"} <= titles


def test_the_original_panels_are_untouched():
    titles = [p["title"] for p in DASH["panels"]]
    for t in ("Confidence of the last diagnosis", "Incidents received", "Connection pool utilisation", "Database disk usage", "Container memory vs limit"):
        assert t in titles
