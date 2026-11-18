"""The fault console only starts or stops the scenario runner. These tests prove the guards around that: the access token, the
typed LIVE confirmation, the switch that keeps live runs off unless the server allows them, one run at a time, and that the
command it builds is the runner's own (so every runner safety rule still applies)."""
import importlib.util
import json
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("fault_console", ROOT / "scripts" / "fault_console" / "console.py")
fc = importlib.util.module_from_spec(_spec)
sys.modules["fault_console"] = fc
_spec.loader.exec_module(fc)


# ---------------------------------------------------------------------------------------------------- validation
@pytest.mark.parametrize("body,allow,err", [
    ({"scenario": "pool"}, False, None),                                            # simulate is the default
    ({"scenario": "disk", "mode": "simulate"}, False, None),
    ({"scenario": "memory", "mode": "live", "confirm": "LIVE"}, True, None),
    ({"scenario": "memory", "mode": "live", "confirm": "LIVE"}, False, "switched off"),   # server does not allow live
    ({"scenario": "memory", "mode": "live"}, True, "confirmation word"),            # no typed confirmation
    ({"scenario": "memory", "mode": "live", "confirm": "live"}, True, "confirmation word"),
    ({"scenario": "rm -rf /"}, True, "unknown scenario"),
    ({"scenario": "unknown"}, True, "unknown scenario"),                            # only the faults the runner lists can be started
    ({"scenario": "down:database", "mode": "live", "confirm": "LIVE"}, True, None),   # a variation is a scenario like any other
    ({"scenario": "rollout:gw-probe"}, False, None), ({"scenario": "unknown:partition; rm -rf /"}, True, "unknown scenario"),
    ({"scenario": "pool", "mode": "yolo"}, True, "unknown mode"),
])
def test_requests_are_validated(body, allow, err):
    scenario, mode, got = fc.validate_request(body, allow)
    if err is None:
        assert got is None and scenario == body["scenario"]
    else:
        assert got and err in got and scenario is None and mode is None


def test_simulate_never_adds_the_live_flag_and_live_does(tmp_path):
    sim = fc.build_command(ROOT, tmp_path, "pool", "simulate")
    live = fc.build_command(ROOT, tmp_path, "pool", "live")
    assert "--live" not in sim and live[-1] == "--live"
    for cmd in (sim, live):
        assert cmd[1].endswith("run_scenarios.py") and cmd[cmd.index("--scenarios") + 1] == "pool"
        assert "--collect" not in cmd


def test_log_summary_uses_only_what_the_runner_said():
    lines = ["[10:00:00] preflight: deployments, monitoring, scorer", "[10:01:00] === pool: injecting (real: x)",
             "[10:04:00]     -> dry_run: all checks passed but live execution is off, so nothing was fixed (not a recovery)",
             "[10:09:00] restoring: AUTO_REMEDIATE off"]
    s = fc.summarize_log(lines)
    assert s["phase"] == "restoring safe settings" and s["outcome"].startswith("dry_run")
    assert fc.summarize_log(lines + ["[10:10:00] done: 0 passed"])["phase"] == "finished"
    stopped = fc.summarize_log(["[10:00:00] STOPPED: alerts are still firing before the first fault"])
    assert stopped["phase"] == "stopped" and "still firing" in stopped["outcome"]
    assert fc.summarize_log([]) == {"phase": "starting", "outcome": None}


def test_token_comparison():
    assert fc.token_ok("abc", "abc")
    assert not fc.token_ok("abd", "abc") and not fc.token_ok("", "abc") and not fc.token_ok(None, "abc")


def test_the_page_does_not_write_untrusted_text_as_html():
    page = (ROOT / "scripts" / "fault_console" / "page.html").read_text(encoding="utf-8")
    assert "innerHTML" not in page and "textContent" in page


# ---------------------------------------------------------------------------------------------------- the HTTP surface
@pytest.fixture
def server(tmp_path, monkeypatch):
    console = fc.Console(ROOT, tmp_path, "s3cret", allow_live=False)
    started = []
    monkeypatch.setattr(console, "start", lambda scenario, mode: (started.append((scenario, mode)) or (True, "started")))
    monkeypatch.setattr(console, "auto_remediate", lambda: "false")
    monkeypatch.setattr(console, "runner_pids", lambda: [])
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), fc.make_handler(console))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", console, started
    httpd.shutdown()


def call(base, path, body=None, token=None, method=None):
    req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", **({"X-Token": token} if token else {})}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def test_every_action_needs_the_token(server):
    base, _, started = server
    assert call(base, "/api/run", {"scenario": "pool"})[0] == 401
    assert call(base, "/api/run", {"scenario": "pool"}, token="wrong")[0] == 401
    assert call(base, "/api/stop", {})[0] == 401
    assert started == []


def test_a_simulated_run_starts_and_a_live_one_is_refused_by_default(server):
    base, _, started = server
    assert call(base, "/api/run", {"scenario": "pool", "mode": "simulate"}, token="s3cret")[0] == 200
    code, body = call(base, "/api/run", {"scenario": "pool", "mode": "live", "confirm": "LIVE"}, token="s3cret")
    assert code == 400 and "switched off" in body["error"]
    assert started == [("pool", "simulate")]


def test_status_reports_the_safe_state_and_the_page_is_served(server):
    base, _, _ = server
    code, body = call(base, "/api/status")
    assert code == 200 and body["auto_remediate"] == "false" and body["running"] is False and {"pool", "memory", "disk"} <= set(body["scenarios"])
    assert {f["family"] for f in body["families"]} >= {"pool", "memory", "disk", "service_down", "bad_rollout", "unknown"}
    assert body["allow_live"] is False
    with urllib.request.urlopen(base + "/", timeout=10) as resp:
        assert b"Fault Console" in resp.read()


def test_only_one_run_at_a_time(tmp_path, monkeypatch):
    console = fc.Console(ROOT, tmp_path, "t", allow_live=True)
    monkeypatch.setattr(console, "runner_pids", lambda: ["123"])        # a runner is already going (maybe not started here)
    ok, msg = console.start("pool", "simulate")
    assert ok is False and "already in progress" in msg


def test_the_status_never_waits_for_kubectl(tmp_path, monkeypatch):
    """A slow cluster must not be able to hang the status page: status() reads a cached value and runs no kubectl."""
    console = fc.Console(ROOT, tmp_path, "t", allow_live=False)
    monkeypatch.setattr(console, "runner_pids", lambda: [])

    def boom(*a, **k):
        raise AssertionError("status() must not run a subprocess")
    monkeypatch.setattr(fc.subprocess, "run", boom)
    st = console.status()
    assert st["auto_remediate"] == "starting" and st["running"] is False


def test_the_refresher_keeps_the_last_good_value_when_kubectl_fails(tmp_path, monkeypatch):
    console = fc.Console(ROOT, tmp_path, "t", allow_live=False)

    class Done:                                                 # what subprocess.run returns
        stdout = "false " + chr(10)
    monkeypatch.setattr(fc.subprocess, "run", lambda *a, **k: Done())
    console.refresh_auto()
    assert console.auto_remediate() == "false"

    def slow(*a, **k):
        raise TimeoutError("kubectl took too long")
    monkeypatch.setattr(fc.subprocess, "run", slow)
    console.refresh_auto()
    assert console.auto_remediate() == "false"                  # not replaced by a guess


# ---------------------------------------------------------------------------------------------------- the engine switches
@pytest.mark.parametrize("body,err", [
    ({"setting": "second_opinion", "value": "gate"}, None), ({"setting": "second_opinion", "value": "shadow"}, None),
    ({"setting": "ambiguity", "value": "on"}, None), ({"setting": "ambiguity", "value": "off"}, None),
    ({"setting": "AUTO_REMEDIATE", "value": "true"}, "unknown setting"),            # the console cannot be used to turn on live remediation
    ({"setting": "ambiguity", "value": "true"}, "unknown value"), ({"setting": "second_opinion", "value": "x; rm -rf /"}, "unknown value"), ({}, "unknown setting"),
])
def test_engine_changes_are_validated(body, err):
    setting, value, got = fc.validate_engine_change(body)
    assert (got is None) if err is None else (got and err in got and setting is None)


def test_each_switch_maps_to_the_right_kubectl_argument():
    assert fc.engine_env_arg("second_opinion", "gate") == "SECOND_OPINION_MODE=gate"
    assert fc.engine_env_arg("second_opinion", "shadow") == "SECOND_OPINION_MODE=shadow"
    assert fc.engine_env_arg("ambiguity", "on") == "ESCALATE_ON_AMBIGUITY=true"
    assert fc.engine_env_arg("ambiguity", "off") == "ESCALATE_ON_AMBIGUITY-"          # unset, exactly how the runner and bring-up reset it


def test_the_switches_are_read_from_the_engine_deployment():
    raw = json.dumps([{"name": "SECOND_OPINION_MODE", "value": "gate"}, {"name": "ESCALATE_ON_AMBIGUITY", "value": "true"}, {"name": "X", "value": "1"}])
    assert fc.parse_engine_env(raw) == {"second_opinion": "gate", "ambiguity": "on"}
    assert fc.parse_engine_env("[]") == {"second_opinion": "shadow", "ambiguity": "off"}        # not set means the safe default
    assert fc.parse_engine_env("not json") == {} and fc.parse_engine_env("") == {"second_opinion": "shadow", "ambiguity": "off"}


def test_a_switch_is_refused_while_a_run_is_in_progress_and_changes_nothing(tmp_path, monkeypatch):
    console = fc.Console(ROOT, tmp_path, "t", allow_live=False)
    monkeypatch.setattr(console, "runner_pids", lambda: ["99"])

    def boom(*a, **k):
        raise AssertionError("kubectl must not run")
    monkeypatch.setattr(fc.subprocess, "run", boom)
    ok, msg = console.set_engine("second_opinion", "gate")
    assert ok is False and "in progress" in msg


def test_a_switch_runs_exactly_one_kubectl_set_env(tmp_path, monkeypatch):
    console = fc.Console(ROOT, tmp_path, "t", allow_live=False)
    monkeypatch.setattr(console, "runner_pids", lambda: [])
    calls = []

    class Done:
        returncode, stderr = 0, ""
    monkeypatch.setattr(fc.subprocess, "run", lambda cmd, **k: (calls.append(cmd) or Done()))
    console._engine = {"second_opinion": "shadow", "ambiguity": "off"}
    ok, _ = console.set_engine("ambiguity", "on")
    assert ok and calls[0] == ["kubectl", "-n", "autosre", "set", "env", "deploy/correlation-engine", "ESCALATE_ON_AMBIGUITY=true"]
    assert len(calls) == 2 and calls[1][:3] == ["kubectl", "create", "--raw"] and "/correlation-engine:8000/proxy/events" in calls[1][3]       # the change is also recorded in the event log
    assert console.engine_switches() == {"second_opinion": "shadow", "ambiguity": "on"}


def test_the_switch_endpoint_needs_the_token_and_status_reports_the_switches(server, monkeypatch):
    base, console, _ = server
    seen = []
    monkeypatch.setattr(console, "set_engine", lambda s, v: (seen.append((s, v)) or (True, "ok")))
    console._engine = {"second_opinion": "shadow", "ambiguity": "off"}
    assert call(base, "/api/engine", {"setting": "ambiguity", "value": "on"})[0] == 401
    assert call(base, "/api/engine", {"setting": "ambiguity", "value": "on"}, token="wrong")[0] == 401
    assert call(base, "/api/engine", {"setting": "AUTO_REMEDIATE", "value": "true"}, token="s3cret")[0] == 400
    assert seen == []
    assert call(base, "/api/engine", {"setting": "ambiguity", "value": "on"}, token="s3cret")[0] == 200 and seen == [("ambiguity", "on")]
    assert call(base, "/api/status")[1]["engine"] == {"second_opinion": "shadow", "ambiguity": "off"}


def test_the_page_explains_both_switches_in_plain_words():
    page = (ROOT / "scripts" / "fault_console" / "page.html").read_text(encoding="utf-8")
    for text in ("ML second opinion", "Ambiguity rule", "Shadow", "Gate", "escalate", "/api/engine"):
        assert text in page


def test_every_variation_is_offered_with_what_to_expect_and_nothing_else_can_be_started():
    fams = fc.families()
    keys = [v["key"] for f in fams for v in f["variants"]]
    assert set(keys) == set(fc.SCENARIOS) and len(keys) == len(set(keys)) >= 17
    for f in fams:
        for v in f["variants"]:
            assert v["expect"] in ("act", "escalate") and v["label"] and v["kind"].startswith("real:")
            assert (v["playbook"] is None) == (v["expect"] == "escalate" and v["key"].startswith("unknown:"))


def test_the_page_offers_a_variation_choice_per_family():
    page = (ROOT / "scripts" / "fault_console" / "page.html").read_text(encoding="utf-8")
    assert "s.families" in page and "createElement('select')" in page and "to be escalated" in page and "innerHTML" not in page
