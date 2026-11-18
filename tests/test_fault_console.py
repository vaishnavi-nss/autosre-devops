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
    ({"scenario": "unknown"}, True, "unknown scenario"),                            # only the three real faults can be started
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
    assert code == 200 and body["auto_remediate"] == "false" and body["running"] is False and set(body["scenarios"]) == {"pool", "memory", "disk"}
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
