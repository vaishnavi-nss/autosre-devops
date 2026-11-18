"""The Fault Replay page on the main website. Its stage logic is pure JavaScript, so it is run here under node with real-shaped incident,
dispatcher and alert data: a stage may only look green when the data really says so (a dry run must never look like a recovery)."""
import asyncio
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from services.frontend import app as fe

SRC = (Path(__file__).resolve().parents[1] / "services" / "frontend" / "app.py").read_text(encoding="utf-8")
NODE = shutil.which("node")


def page() -> str:
    return asyncio.run(fe.serve_dashboard())


def stages(inc, executions=None, alerts=None):
    fn = SRC[SRC.index("function replayStages"):SRC.index("function fmtTime")]
    script = fn + "\nconsole.log(JSON.stringify(replayStages(%s, %s, %s)));" % (json.dumps(inc), json.dumps(executions or []), json.dumps(alerts or []))
    out = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30, check=True).stdout
    return json.loads(out)


def test_the_page_has_the_replay_view_and_escapes_what_it_shows():
    html = page()
    assert 'id="view-replay"' in html and 'id="nav-replay"' in html and "switchView('replay')" in html
    replay = html[html.index("function renderReplay"):html.index("async function pollState")]
    assert "esc(" in replay and "textContent" in replay                 # values from the cluster go through esc()
    for field in ("s.d", "s.t", "l.text", "i.root_cause"):
        assert re.search(r"esc\(" + re.escape(field), replay), field


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_no_incident_shows_every_stage_waiting():
    st = stages(None)
    assert len(st) == 8 and {s["s"] for s in st} == {"wait"}


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_a_dry_run_never_looks_like_a_recovery():
    inc = {"root_cause": "db_connection_pool_exhaustion", "service": "transaction-service", "confidence": 0.85, "action": "restart", "playbook": "restart_service.yml",
           "alerts": [{"alertname": "DBPoolNearExhaustion"}], "second_opinion": {"verdict": "agree", "scorer_fault": "db_connection_pool_exhaustion"},
           "remediation": {"status": "dry_run", "reason": "live execution is off"}}
    st = stages(inc)
    assert [s["s"] for s in st[:5]] == ["done", "done", "done", "done", "done"]
    assert st[5]["s"] == "wait" and st[6]["s"] == "wait" and st[7]["s"] == "wait"       # nothing ran, nothing verified


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_a_verified_fix_lights_every_stage_only_when_the_alerts_are_gone():
    inc = {"root_cause": "high_memory_usage", "service": "transaction-service", "confidence": 0.75, "action": "scale", "alerts": [{"alertname": "HighMemory"}],
           "second_opinion": {"verdict": "agree"}, "remediation": {"status": "accepted", "execution_id": "abc123"}}
    ex = [{"id": "abc123", "playbook": "scale_replicas.yml", "status": "succeeded", "duration_s": 24.4}]
    assert [s["s"] for s in stages(inc, ex, [])] == ["done"] * 8
    still = stages(inc, ex, [{"alertname": "HighMemory"}])
    assert still[6]["s"] == "done" and still[7]["s"] == "warn"                         # verified, but alerts are still clearing


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_failures_and_escalations_are_red_not_green():
    unknown = stages({"root_cause": None, "action": "escalate_to_human", "reason": "no signature", "alerts": [{"alertname": "X"}]})
    assert unknown[2]["s"] == "stop" and unknown[4]["s"] == "stop" and unknown[6]["s"] == "wait"
    disagree = stages({"root_cause": "db_disk_full", "confidence": 0.85, "alerts": [], "second_opinion": {"verdict": "disagree"},
                       "remediation": {"status": "held_back", "reason": "second opinion disagrees"}})
    assert disagree[3]["s"] == "stop" and disagree[4]["s"] == "stop"
    bad = stages({"root_cause": "db_disk_full", "confidence": 0.85, "alerts": [], "second_opinion": {"verdict": "agree"},
                  "remediation": {"status": "accepted", "execution_id": "z"}}, [{"id": "z", "playbook": "clear_logs.yml", "status": "verification_failed"}])
    assert bad[5]["s"] == "stop" and bad[6]["s"] == "stop"


# ---------------------------------------------------------------------------------------------------- transparency mode
def variant_rows(inc):
    fn = SRC[SRC.index("function variantRows"):SRC.index("function renderVariants")]
    script = fn + "\nconsole.log(JSON.stringify(variantRows(%s)));" % json.dumps(inc)
    return json.loads(subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30, check=True).stdout)


def test_the_header_has_the_transparency_switch_off_by_default_and_remembers_the_choice():
    html = page()
    assert 'id="tm-btn"' in html and 'role="switch"' in html and 'aria-checked="false"' in html and "Transparency mode" in html
    assert html.index('id="tm-btn"') > html.index('id="nav-replay"')                 # in the header, after the navigation (top right)
    assert "body.tm-on .tm-panel { display: block; }" in html and ".tm-panel { display: none;" in html
    assert "localStorage" in html and "try {" in html
    assert 'id="tm-exec"' in html and 'id="tm-replay"' in html


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_every_setting_is_listed_with_its_own_decision_whatever_the_real_outcome():
    inc = {"variants": {"second_opinion_verdict": "disagree", "ambiguous_causes": [], "rows": [
        {"key": "rules", "label": "Rules only", "action": "act", "playbook": "restart_service.yml", "cause": "db_connection_pool_exhaustion", "reason": "decided by the rules alone", "active": True},
        {"key": "ml", "label": "Rules + second opinion", "action": "escalate", "cause": "db_connection_pool_exhaustion", "reason": "the second opinion disagrees", "active": False}]}}
    rows = variant_rows(inc)
    assert [r["label"] for r in rows] == ["Rules only", "Rules + second opinion"]
    assert rows[0]["decision"] == "Acts: restart_service.yml" and rows[0]["active"] is True and rows[0]["escalate"] is False
    assert rows[1]["decision"] == "Escalates to a person" and rows[1]["escalate"] is True and rows[1]["why"] == "the second opinion disagrees"
    assert variant_rows({}) == [] and variant_rows(None) == []


def test_the_variant_panel_writes_cluster_text_through_esc():
    block = SRC[SRC.index("function renderVariants"):SRC.index("function setTransparency")]
    for field in ("r.label", "r.decision", "r.why", "v.second_opinion_verdict"):
        assert "esc(" + field in block, field
