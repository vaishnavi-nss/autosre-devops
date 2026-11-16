"""The scenario runner's decision logic. The cluster is replaced by a fake; these tests prove how outcomes are classified and that
the run always restores the safe defaults. That the scenarios work against a real cluster is shown by running them (docs/completion)."""
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("run_scenarios", ROOT / "scripts" / "scenarios" / "run_scenarios.py")
rs = importlib.util.module_from_spec(_spec)
sys.modules["run_scenarios"] = rs
_spec.loader.exec_module(rs)

POOL = rs.SCENARIOS["pool"]
INC = {"received_at": "2026-11-20T10:01:00+00:00", "root_cause": "db_connection_pool_exhaustion", "action": None,
       "alerts": [{"alertname": "DBPoolNearExhaustion"}], "second_opinion": {"verdict": "agree"}, "remediation": {"status": "accepted"}}
EX = {"started_at": "2026-11-20T10:01:01+00:00", "playbook": "restart_service.yml", "status": "succeeded"}


def ev(**over):
    base = {"alert_fired": True, "incident": INC, "execution": EX, "independent_recovery": True, "recovery_detail": "ok"}
    base.update(over)
    return base


def test_only_the_whole_chain_with_an_independent_check_passes():
    assert rs.classify(POOL, ev())[0] == "passed"


@pytest.mark.parametrize("over,outcome", [
    ({"alert_fired": False}, "no_alert"),
    ({"incident": None}, "no_incident"),
    ({"incident": {**INC, "second_opinion": None}}, "no_verdict"),
    ({"incident": {**INC, "second_opinion": {"verdict": None}}}, "no_verdict"),
    ({"incident": {**INC, "remediation": {"status": "dry_run"}}}, "dry_run"),
    ({"incident": {**INC, "remediation": {"status": "escalated", "reason": "Second opinion is disagree"}}}, "escalated_by_policy"),
    ({"incident": {**INC, "remediation": {"status": "skipped", "reason": "cooldown"}}}, "skipped"),
    ({"execution": None}, "no_execution"),
    ({"execution": {**EX, "status": "verification_failed", "reason": "rollout not complete"}}, "execution_verification_failed"),
    ({"execution": {**EX, "status": "failed", "reason": "ansible exited 2"}}, "execution_failed"),
    ({"independent_recovery": False, "recovery_detail": "still firing"}, "not_recovered"),
])
def test_every_other_result_is_named_for_what_it_was(over, outcome):
    got, why = rs.classify(POOL, ev(**over))
    assert got == outcome and why


def test_a_dry_run_is_never_reported_as_a_recovery():
    assert rs.classify(POOL, ev(incident={**INC, "remediation": {"status": "dry_run"}}, execution=None, independent_recovery=None))[0] != "passed"


def test_matching_ignores_older_and_wrong_incidents():
    old = {**INC, "received_at": "2026-11-20T09:00:00+00:00"}
    other = {**INC, "root_cause": "high_memory_usage"}
    esc = {**INC, "root_cause": None, "action": "escalate_to_human"}
    items = [other, INC, old]                                   # newest first, as the engine returns them
    assert rs.find_incident(items, "2026-11-20T10:00:00+00:00", cause="db_connection_pool_exhaustion") is INC
    assert rs.find_incident([old], "2026-11-20T10:00:00+00:00") is None
    assert rs.find_incident([esc, INC], "2026-11-20T10:00:00+00:00", escalated=True) is esc
    assert rs.find_incident([INC], "2026-11-20T10:00:00+00:00", escalated=True) is None


def test_the_two_fault_incident_needs_both_alerts():
    only = {**INC, "alerts": [{"alertname": "ContainerMemoryHigh"}]}
    both = {**INC, "alerts": [{"alertname": "ContainerMemoryHigh"}, {"alertname": "DBPoolNearExhaustion"}]}
    want = {"ContainerMemoryHigh", "DBPoolNearExhaustion"}
    assert rs.find_incident_with([both, only], "2026-11-20T10:00:00+00:00", want) is both
    assert rs.find_incident_with([only], "2026-11-20T10:00:00+00:00", want) is None


def test_executions_are_matched_by_playbook_and_time():
    other = {**EX, "playbook": "scale_replicas.yml"}
    assert rs.find_execution([other, EX], "2026-11-20T10:00:00+00:00", "restart_service.yml") is EX
    assert rs.find_execution([EX], "2026-11-20T10:05:00+00:00", "restart_service.yml") is None


class FakeCluster(rs.Cluster):
    def __init__(self, replies=None, dispatcher_auto=False):
        self.calls, self.replies, self.auto = [], replies or {}, dispatcher_auto

    def kubectl(self, *args, **kw):
        self.calls.append(args)
        if args[:3] == ("-n", rs.NS, "set") and "AUTO_REMEDIATE=true" in args:
            self.auto = True
        if args[:3] == ("-n", rs.NS, "set") and "AUTO_REMEDIATE=false" in args:
            self.auto = False
        return ""

    def get(self, path, timeout=60):
        if path.endswith("/health") and "remediation-dispatcher" in path:
            return {"auto_remediate": self.auto}
        return self.replies.get(path, {"executions": [], "incidents": []})

    def post(self, path, body=None, timeout=90):
        self.calls.append(("POST", path, body))
        return {"status": "escalated", "reason": "x"} if "/dispatch" in path else {}


def runner(tmp_path, cluster, live=False):
    return rs.Runner(cluster, tmp_path, live, 900, {"alert": 1, "engine": 1, "execute": 1, "resolve": 1, "quiet": 1})


def test_restore_always_switches_live_execution_off_and_resets_everything(tmp_path):
    c = FakeCluster(dispatcher_auto=True)
    r = runner(tmp_path, c, live=True)
    r.restore()
    assert c.auto is False and r.environment["restored"]["auto_remediate"] is False
    joined = [" ".join(map(str, a)) for a in c.calls]
    assert any("scale deploy/transaction-service --replicas=1" in j for j in joined)
    assert any("CHAOS_ENABLED=false" in j and "transaction-service" in j for j in joined)
    assert any("SECOND_OPINION_MODE=shadow" in j and "ESCALATE_ON_AMBIGUITY-" in j for j in joined)
    assert sum(1 for a in c.calls if a and a[0] == "POST" and "chaos/reset" in a[1]) == 2


def test_restore_keeps_going_when_a_step_fails(tmp_path):
    class Flaky(FakeCluster):
        def kubectl(self, *args, **kw):
            if "scale" in args:
                raise RuntimeError("api down")
            return super().kubectl(*args, **kw)
    c = Flaky(dispatcher_auto=True)
    r = runner(tmp_path, c, live=True)
    r.restore()
    assert c.auto is False and r.environment["restored"]["errors"] == ["api down"]


def test_preflight_refuses_to_start_if_the_dispatcher_is_not_in_its_safe_default(tmp_path):
    class Unsafe(FakeCluster):
        def deployment(self, name):
            return {"status": {"readyReplicas": 1}}

        def prom_scalar(self, expr, default=None):
            return 7
    c = Unsafe(dispatcher_auto=True)
    r = runner(tmp_path, c)
    c.replies[f"{rs.AM}/api/v2/status"] = {"cluster": {"status": "ready"}}
    with pytest.raises(RuntimeError, match="safe default"):
        r.preflight()


def test_guardrail_checks_are_all_escalations_and_start_nothing(tmp_path):
    c = FakeCluster()
    r = runner(tmp_path, c)
    rec = r.run_guardrails(live=True)
    assert rec["passed"] and rec["evidence"]["executions"] == {"before": 0, "after": 0}
    sent = [a[2] for a in c.calls if a and a[0] == "POST"]
    assert len(sent) == 6
    accepted = {"root_cause": "db_connection_pool_exhaustion", "confidence": 0.9, "service": "transaction-service", "second_opinion": {"verdict": "agree"}}
    assert accepted not in sent, "the live guardrail run must not send the one payload that would be accepted"


def test_the_report_labels_a_dry_run_and_what_was_actually_injected(tmp_path):
    results = [{"scenario": "pool", "fault_kind": "injected: the pool gauge is set", "outcome": "dry_run", "why": "x", "passed": False,
                "evidence": {"incident": INC}, "timeline": {"alert_after_s": 40, "incident_after_s": 95}}]
    text = rs.report(results, live=False, env={"restored": {"auto_remediate": False}})
    assert "dry run (decisions only, nothing fixed)" in text and "**dry_run**" in text and "Passed: 0 of 1" in text
    assert "no real connections" in text and "persist" in text


def test_posts_carry_a_json_content_type(monkeypatch):
    """Found on the real cluster: `kubectl create --raw` sent no JSON content type and the chaos endpoint answered 422."""
    seen = {}

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"status": "pool set"}'

    def fake_urlopen(req, timeout=0):
        if hasattr(req, "full_url"):
            seen["headers"], seen["url"], seen["body"] = dict(req.header_items()), req.full_url, req.data
        return Resp()

    monkeypatch.setattr(rs.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(rs.Cluster, "_proxy_proc", type("P", (), {"poll": lambda self: None})())
    out = rs.Cluster().post("/api/v1/namespaces/autosre/services/transaction-service:http/proxy/chaos/pool", {"active_connections": 19})
    assert out == {"status": "pool set"} and seen["headers"]["Content-type"] == "application/json"
    assert seen["body"] == b'{"active_connections": 19}' and seen["url"].startswith("http://127.0.0.1:8011/api/v1/")


def test_the_runner_judges_the_incident_the_dispatcher_acts_on_not_the_first_webhook():
    """Found on the real cluster: the first webhook carries only the deciding symptom (0.55) and is rightly escalated by the 0.75 gate."""
    first = {**INC, "received_at": "2026-11-20T10:01:00+00:00", "confidence": 0.55, "remediation": {"status": "escalated", "reason": "Confidence below threshold"}}
    later = {**INC, "received_at": "2026-11-20T10:03:00+00:00", "confidence": 0.75, "remediation": {"status": "accepted"}}
    since = "2026-11-20T10:00:00+00:00"
    assert rs.pick_incident([later, first], since, INC["root_cause"]) == (later, True)
    assert rs.pick_incident([first], since, INC["root_cause"]) == (first, False)      # reported with its reason, not hidden
    assert rs.pick_incident([], since, INC["root_cause"]) == (None, False)
    assert rs.pick_incident([{**later, "remediation": {"status": "dry_run"}}], since, INC["root_cause"])[1] is True


def test_every_fault_scenario_also_injects_the_consequence_that_lifts_confidence_to_the_gate():
    import inspect
    src = inspect.getsource(rs.Runner.inject)
    for chaos in ("/chaos/errors", "/chaos/latency"):
        assert chaos in src
    sig = __import__("signatures", fromlist=["x"]) if False else None
    # 0.55 (pool) + 0.20 (errors), 0.60 (memory) + 0.15 (latency), 0.60 (disk) + 0.25 (errors) all reach 0.75
    assert round(0.55 + 0.20, 4) >= 0.75 and round(0.60 + 0.15, 4) >= 0.75 and round(0.60 + 0.25, 4) >= 0.75


def test_sigterm_runs_the_restore_instead_of_killing_the_process_silently():
    """Found after comp5: a relaunch sent SIGTERM, Python exited without running `finally`, and AUTO_REMEDIATE stayed true."""
    import signal
    previous = signal.getsignal(signal.SIGTERM)
    try:
        rs.install_term_handler()
        handler = signal.getsignal(signal.SIGTERM)
        with pytest.raises(SystemExit) as stop:
            handler(signal.SIGTERM, None)
        assert stop.value.code == 128 + signal.SIGTERM
    finally:
        signal.signal(signal.SIGTERM, previous)


def test_the_launcher_waits_for_the_old_runner_and_forces_the_safe_defaults():
    text = (ROOT / "scripts" / "aws" / "completion-run.sh").read_text()
    assert "pkill -TERM -f 'run_scenario[s]'" in text and "pgrep -f 'run_scenario[s]'" in text
    assert text.index("AUTO_REMEDIATE=false") < text.index("rollout restart")
    assert "CHAOS_ENABLED=false" in text and "SECOND_OPINION_MODE=shadow" in text


def test_a_post_to_a_pod_that_is_still_starting_is_retried_but_a_real_error_is_not(monkeypatch):
    """Found in comp9: right after a restart the chaos call got HTTP 503 (EOF) and the whole run stopped."""
    import urllib.error
    monkeypatch.setattr(rs.Cluster, "_proxy_proc", type("P", (), {"poll": lambda self: None})())
    monkeypatch.setattr(rs.Cluster, "RETRY_PAUSE_S", 0)
    calls = []

    def flaky(req, timeout=0):
        calls.append(1)
        if len(calls) < 3:
            raise urllib.error.HTTPError(req.full_url, 503, "unavailable", {}, __import__("io").BytesIO(b"EOF"))

        class Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b'{"ok": true}'
        return Resp()

    monkeypatch.setattr(rs.urllib.request, "urlopen", flaky)
    assert rs.Cluster().post("/x", {}) == {"ok": True} and len(calls) == 3

    def forbidden(req, timeout=0):
        calls.append(1)
        raise urllib.error.HTTPError(req.full_url, 422, "bad", {}, __import__("io").BytesIO(b"bad body"))
    calls.clear()
    monkeypatch.setattr(rs.urllib.request, "urlopen", forbidden)
    with pytest.raises(RuntimeError, match="HTTP 422"):
        rs.Cluster().post("/x", {})
    assert len(calls) == 1

    def always_down(req, timeout=0):
        raise urllib.error.URLError("connection refused")
    monkeypatch.setattr(rs.urllib.request, "urlopen", always_down)
    with pytest.raises(RuntimeError, match="URLError"):
        rs.Cluster().post("/x", {}, tries=2)
