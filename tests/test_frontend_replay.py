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
