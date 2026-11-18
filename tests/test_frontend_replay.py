"""The merged website: header controls, the live fault tracker (run under node), the history builder and the history endpoints.

A stage may only look done when an event says so (a dry run must never look like a recovery), a step in progress must show as active, and the
replay of a finished incident must show exactly the events up to its moving clock."""
import asyncio
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from services.frontend import app as fe
from services.frontend import history

ROOT = Path(__file__).resolve().parents[1]
JS = ROOT / "services" / "frontend" / "static" / "console.js"
NODE = shutil.which("node")
client = TestClient(fe.app)
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")


def run_js(expr: str):
    script = f"const c = require({json.dumps(str(JS))}); console.log(JSON.stringify({expr}));"
    return json.loads(subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30, check=True).stdout)


def page() -> str:
    return asyncio.run(fe.serve_dashboard())


# ---------------------------------------------------------------------------------------------------- the page
def test_the_header_has_roles_theme_transparency_and_sign_in():
    html = page()
    assert 'data-role="executive"' in html and 'data-role="oncall"' in html and "On-call engineer" in html
    assert 'data-theme-set="light"' in html and 'data-theme-set="dark"' in html and html.count("<svg") >= 2          # light and dark, each with an icon
    assert 'id="tm-btn"' in html and 'aria-checked="false"' in html and "Transparency mode" in html
    assert 'id="login"' in html and 'id="signout"' in html and "/static/console.js" in html and "/static/console.css" in html
    assert html.index('id="tm-btn"') > html.index('id="role-seg"')


def test_it_lands_on_the_original_executive_overview_and_keeps_the_original_pages():
    html = page()
    assert 'id="view-dashboard" class="view-section active"' in html and 'id="view-status"' in html
    assert "Transaction Dependency Architecture Topology" in html and "DB Pool Saturation" in html
    for view in ("incidents", "replay", "trends", "services", "playbooks", "settings", "runs"):
        assert f'id="view-{view}"' in html


def test_the_original_parchment_is_the_light_theme_and_dark_is_a_palette_on_the_same_tokens():
    html = page()
    assert "--parchment-bg: #EAE5DC" in html and 'html[data-theme="dark"]' in html and "--title-ink" in html
    assert "localStorage.getItem('autosre-theme')" in html                      # applied before the page paints, so there is no flash


def test_static_files_are_served_and_cluster_text_goes_through_esc():
    assert client.get("/static/console.js").status_code == 200 and client.get("/static/console.css").status_code == 200
    src = JS.read_text(encoding="utf-8")
    for field in ("s.title", "s.text", "e.text", "i.reason", "i.service", "r.why"):
        assert "esc(" + field in src, field


# ---------------------------------------------------------------------------------------------------- the live tracker
EV = [
    {"stage": 1, "state": "done", "t": "2026-11-20T10:00:00Z", "text": "alerts"}, {"stage": 2, "state": "done", "t": "2026-11-20T10:00:40Z", "text": "grouped"},
    {"stage": 3, "state": "done", "t": "2026-11-20T10:00:40Z", "text": "diagnosed"}, {"stage": 4, "state": "done", "t": "2026-11-20T10:00:40Z", "text": "agree"},
    {"stage": 5, "state": "done", "t": "2026-11-20T10:00:41Z", "text": "gate"}, {"stage": 6, "state": "start", "t": "2026-11-20T10:00:42Z", "text": "restart started"},
    {"stage": 6, "state": "done", "t": "2026-11-20T10:01:10Z", "text": "restart finished"}, {"stage": 7, "state": "start", "t": "2026-11-20T10:01:10Z", "text": "verifying"},
    {"stage": 7, "state": "done", "t": "2026-11-20T10:01:25Z", "text": "verified"}, {"stage": 8, "state": "done", "t": "2026-11-20T10:01:26Z", "text": "normal"}]


def at(ts: str) -> int:
    from datetime import datetime
    return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000)


@needs_node
def test_each_step_lights_up_only_when_its_event_has_happened():
    def states(ts, final=False):
        return [s["state"] for s in run_js(f"c.trackerStages({json.dumps(EV)}, {at(ts)}, {{final: {str(final).lower()}}})")]
    assert states("2026-11-20T10:00:10Z") == ["done", "active", "wait", "wait", "wait", "wait", "wait", "wait"]       # alerts noticed, grouping in progress
    assert states("2026-11-20T10:00:50Z") == ["done"] * 5 + ["active", "wait", "wait"]                                  # the playbook is running
    assert states("2026-11-20T10:01:15Z") == ["done"] * 6 + ["active", "wait"]                                           # verification in progress
    assert states("2026-11-20T10:02:00Z") == ["done"] * 8


@needs_node
def test_a_dry_run_never_looks_like_a_recovery():
    ev = EV[:5]
    st = run_js(f"c.trackerStages({json.dumps(ev)}, 9e15, {{final: true, reason: 'live remediation was off'}})")
    assert [s["state"] for s in st] == ["done"] * 5 + ["skip"] * 3 and st[5]["text"] == "live remediation was off"


@needs_node
def test_an_unknown_cause_stops_at_diagnosis_and_nothing_after_it_looks_done():
    ev = [{"stage": 1, "state": "done", "t": "2026-11-20T10:00:00Z", "text": "a"}, {"stage": 2, "state": "done", "t": "2026-11-20T10:00:40Z", "text": "b"},
          {"stage": 3, "state": "stop", "t": "2026-11-20T10:00:40Z", "text": "no known cause"}, {"stage": 5, "state": "stop", "t": "2026-11-20T10:00:40Z", "text": "escalated"}]
    st = run_js(f"c.trackerStages({json.dumps(ev)}, 9e15, {{final: true}})")
    assert [s["state"] for s in st] == ["done", "done", "stop", "skip", "skip", "skip", "skip", "skip"]


@needs_node
def test_a_failed_verification_is_red_and_the_ml_disagreement_stops_the_gate():
    bad = [dict(e) for e in EV]
    bad[-2] = {"stage": 7, "state": "stop", "t": "2026-11-20T10:01:25Z", "text": "recovery could not be verified"}
    bad = bad[:-1]
    st = run_js(f"c.trackerStages({json.dumps(bad)}, 9e15, {{final: true}})")
    assert st[6]["state"] == "stop" and st[7]["state"] == "skip"
    veto = [EV[0], EV[1], EV[2], {"stage": 4, "state": "stop", "t": "2026-11-20T10:00:40Z", "text": "ML disagrees"}, {"stage": 5, "state": "stop", "t": "2026-11-20T10:00:40Z", "text": "held back"}]
    st = run_js(f"c.trackerStages({json.dumps(veto)}, 9e15, {{final: true}})")
    assert st[3]["state"] == "stop" and all(s["state"] == "skip" for s in st[4:])


@needs_node
def test_a_fixed_incident_is_only_back_to_normal_when_the_alerts_are_gone():
    st = run_js(f"c.trackerStages({json.dumps(EV)}, 9e15, {{final: true, alertsQuiet: false}})")
    assert st[6]["state"] == "done" and st[7]["state"] == "warn"


@needs_node
def test_buckets_use_days_for_a_short_history_and_weeks_for_a_long_one():
    short = run_js("c.buckets([{t:'2026-11-18T10:00:00Z',action:'auto_fixed'},{t:'2026-11-20T10:00:00Z',action:'escalated'}])")
    assert short["unit"] == "day" and len(short["rows"]) == 3 and short["rows"][0]["auto"] == 1 and short["rows"][2]["esc"] == 1
    long_ = run_js("c.buckets([{t:'2026-01-01T10:00:00Z',action:'dry_run'},{t:'2026-11-20T10:00:00Z',action:'auto_fixed'}])")
    assert long_["unit"] == "week" and long_["rows"][0]["dry"] == 1


@needs_node
def test_transparency_rows_come_from_the_recorded_variants():
    inc = {"variants": {"rows": [{"label": "Rules only", "action": "act", "playbook": "restart_service.yml", "reason": "decided by the rules alone", "active": True},
                                 {"label": "Rules + second opinion", "action": "escalate", "reason": "the second opinion disagrees", "active": False}]}}
    rows = run_js(f"c.variantRows({json.dumps(inc)})")
    assert rows[0]["decision"] == "Acts: restart_service.yml" and rows[0]["active"] and rows[1]["escalate"] and run_js("c.variantRows(null)") == []


# ---------------------------------------------------------------------------------------------------- the history builder
INC = {"id": "INC-0007", "started_at": "2026-11-20T10:00:00+00:00", "received_at": "2026-11-20T10:00:40+00:00", "status": "correlated", "root_cause": "high_memory_usage",
       "service": "transaction-service", "confidence": 0.85, "playbook": "scale_replicas.yml", "alerts": [{"alertname": "ContainerMemoryHigh", "service": "transaction-service"}],
       "second_opinion": {"verdict": "agree"}, "remediation": {"status": "accepted", "execution_id": "e1"}}
EX = {"id": "e1", "playbook": "scale_replicas.yml", "status": "succeeded", "duration_s": 24.4, "started_at": "2026-11-20T10:00:42+00:00", "finished_at": "2026-11-20T10:01:26+00:00",
      "steps": [{"t": "2026-11-20T10:00:42+00:00", "step": "preflight"}, {"t": "2026-11-20T10:00:44+00:00", "step": "ansible"}, {"t": "2026-11-20T10:01:10+00:00", "step": "verify"}, {"t": "2026-11-20T10:01:25+00:00", "step": "recovered"}]}


def test_a_fixed_incident_becomes_auto_fixed_with_a_timestamped_stage_timeline():
    out = history.build([INC], [EX], [])["incidents"][0]
    assert out["action"] == "auto_fixed" and out["ttr_s"] == 86 and out["verified"] is True and out["open"] is False and out["sev"] == "SEV3"
    stages = {(e["stage"], e["state"]) for e in out["events"]}
    assert {(1, "done"), (2, "done"), (3, "done"), (4, "done"), (5, "done"), (6, "start"), (6, "done"), (7, "start"), (7, "done"), (8, "done")} <= stages
    assert all(e["t"] for e in out["events"])


def test_a_running_playbook_has_no_verification_yet_and_a_dry_run_has_no_playbook_events():
    running = dict(EX, status="running", steps=EX["steps"][:2]); running.pop("finished_at")
    out = history.build([INC], [running], [])["incidents"][0]
    assert out["action"] == "running" and not any(e["stage"] in (7, 8) for e in out["events"])
    dry = dict(INC, remediation={"status": "dry_run", "reason": "live execution is off"})
    out = history.build([dry], [], [])["incidents"][0]
    assert out["action"] == "dry_run" and not any(e["stage"] >= 6 for e in out["events"]) and out["ttr_s"] is None


def test_escalations_are_open_until_resolved_and_acks_come_from_events():
    esc = {"id": "INC-0008", "received_at": "2026-11-20T11:00:00+00:00", "status": "unmatched", "action": "escalate_to_human", "root_cause": None, "reason": "no signature", "alerts": [{"alertname": "ErrorRateHigh", "service": "api-gateway"}]}
    out = history.build([esc], [], [])["incidents"][0]
    assert out["action"] == "escalated" and out["open"] and out["assignee"] is None and out["sev"] == "SEV2"
    acked = history.build([esc], [], [{"kind": "ack", "name": "INC-0008", "by": "a.rao", "t": "2026-11-20T11:05:00+00:00"}])["incidents"][0]
    assert acked["assignee"] == "a.rao" and acked["open"] and any(e["text"] == "Acknowledged by a.rao" for e in acked["events"])
    done = history.build([esc], [], [{"kind": "ack", "name": "INC-0008", "by": "a.rao", "t": "2026-11-20T11:05:00+00:00"},
                                     {"kind": "resolve", "name": "INC-0008", "by": "a.rao", "t": "2026-11-20T11:40:00+00:00", "text": "false alarm"}])["incidents"][0]
    assert done["open"] is False and done["note"] == "false alarm" and done["ttr_s"] == 2400


def test_a_vetoed_incident_is_held_and_changes_and_runs_are_separated():
    veto = dict(INC, status="vetoed", root_cause=None, reason="the metrics disagree", remediation=None, second_opinion={"verdict": "disagree"})
    out = history.build([veto], [], [{"kind": "change", "setting": "live_remediation", "value": "on"}, {"kind": "run", "name": "pool"}])
    assert out["incidents"][0]["action"] == "vetoed" and out["incidents"][0]["open"] and len(out["changes"]) == 1 and len(out["runs"]) == 1


# ---------------------------------------------------------------------------------------------------- the endpoints
class Reply:
    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self.data


def test_history_endpoint_joins_engine_dispatcher_and_events(monkeypatch):
    async def fake_json(client_, url, params=None):
        if url.endswith("/incidents"):
            return {"incidents": [INC]}, None
        if url.endswith("/executions"):
            return {"executions": [EX]}, None
        if url.endswith("/events"):
            return {"events": [{"kind": "ack", "name": "INC-0007", "by": "m.silva", "t": "2026-11-20T10:02:00+00:00"}]}, None
        return None, "unexpected"
    monkeypatch.setattr(fe, "_json", fake_json)
    body = client.get("/api/history").json()
    assert body["incidents"][0]["id"] == "INC-0007" and body["incidents"][0]["action"] == "auto_fixed" and body["incidents"][0]["assignee"] == "m.silva" and body["errors"] == {}


def test_history_still_answers_when_a_source_is_down(monkeypatch):
    async def fake_json(client_, url, params=None):
        return (None, "ConnectError: down") if url.endswith("/events") else ({"incidents": [INC]} if url.endswith("/incidents") else {"executions": []}, None)
    monkeypatch.setattr(fe, "_json", fake_json)
    body = client.get("/api/history").json()
    assert len(body["incidents"]) == 1 and "engine" in body["errors"]


def test_ack_and_resolve_are_validated_and_forwarded_to_the_engine(monkeypatch):
    sent = []

    class FakeClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, json=None):
            sent.append((url, json)); return Reply({"id": "EVT-0001"})
    monkeypatch.setattr(fe.httpx, "AsyncClient", FakeClient)
    assert client.post("/api/ack", json={"id": "INC-0007", "by": "a.rao"}).status_code == 200
    assert client.post("/api/ack", json={"id": "x; drop", "by": "a.rao"}).status_code == 422 and client.post("/api/ack", json={"id": "INC-0007", "by": ""}).status_code == 422
    assert client.post("/api/resolve", json={"id": "INC-0007", "by": "<b>a.rao</b>", "note": "n" * 500}).status_code == 200
    assert sent[0][1] == {"kind": "ack", "name": "INC-0007", "by": "a.rao"} and sent[1][1]["by"] == "ba.raob" and len(sent[1][1]["text"]) == 200


@needs_node
def test_with_no_data_every_step_waits_and_nothing_pulses():
    st = run_js("c.trackerStages([], 9e15, {final: false})")
    assert [s["state"] for s in st] == ["wait"] * 8
    live = run_js("c.trackerStages([], 9e15, {final: false, live: true})")
    assert live[0]["state"] == "active" and live[1]["state"] == "wait"            # a fault being noticed right now: only the first step is in progress
