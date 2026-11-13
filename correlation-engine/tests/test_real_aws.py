"""real_aws_validate.py / real_aws_report.py: the scoring rules, the in-container code, and the trial driver's ordering.
Everything that touches the cluster is mocked; no number here is a result."""
from __future__ import annotations

import base64
import inspect
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evaluation"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import real_aws_report as rr        # noqa: E402
import real_aws_validate as rv      # noqa: E402


def test_the_code_sent_into_containers_compiles():
    compile(rv.HOG.format(target=0.93), "hog", "exec")
    compile(rv.BURN, "burn", "exec")
    compile(rv.KILL_MARKED.format(mark="x"), "kill", "exec")
    wrapped = rv.background_code(rv.HOG_MARK, rv.BURN)
    compile(wrapped, "wrapped", "exec")
    assert rv.HOG_MARK in wrapped                                    # visible in the cmdline, so it can be killed by mark
    assert base64.b64encode(rv.BURN.encode()).decode() in wrapped


def test_plan_parsing():
    assert rv.parse_plan("healthy:15,memory_pressure:3,pair:2") == [("healthy", 15), ("memory_pressure", 3), ("pair", 2)]
    assert rv.parse_plan("db_outage") == [("db_outage", 1)]


def test_every_fault_is_real_not_a_chaos_call():
    src = inspect.getsource(rv)
    assert "pod_http" not in src and "post /chaos" not in src.lower()
    assert "/chaos/" not in src.replace("chaos endpoint", "")      # no chaos endpoint path is ever called
    faults = rv.build_faults()
    assert {n for n, f in faults.items() if f.cause} == {"memory_pressure", "pool_exhaustion"}
    assert faults["memory_pressure"].cause == "high_memory_usage"
    assert faults["pool_exhaustion"].cause == "db_connection_pool_exhaustion"


def trial(fault, truth, pred, *, effective=True, mode="single", contaminated=False, **extra):
    decision = {"rule_prediction": pred, "escalated": pred is None, "second_opinion": "agree" if pred else None,
                "gate_would_withhold": False, "ambiguous_causes": [], "ambiguity_rule_would": pred or "escalate",
                "playbook": "restart_service.yml" if pred else None}
    return {"trial_id": 1, "real_fault": fault, "truth": truth, "mode": mode, "contaminated": contaminated,
            "fault_effective": effective, "decisions": [] if pred == "NONE" else [decision], "session": "s", **extra}


def test_outcomes():
    assert rr.outcome(trial("m", ["high_memory_usage"], "high_memory_usage")) == "correct"
    assert rr.outcome(trial("m", ["high_memory_usage"], "db_connection_pool_exhaustion")) == "wrong"
    assert rr.outcome(trial("m", ["high_memory_usage"], None)) == "escalated"
    assert rr.outcome(trial("m", ["high_memory_usage"], "NONE")) == "missed"
    assert rr.outcome(trial("o", [], None)) == "correct"            # no known cause: escalating is right
    assert rr.outcome(trial("o", [], "high_memory_usage")) == "wrong"


def test_only_effective_uncontaminated_single_trials_are_scored():
    assert rr.scored(trial("m", ["high_memory_usage"], "high_memory_usage"))
    assert not rr.scored(trial("m", ["high_memory_usage"], "high_memory_usage", effective=False))
    assert not rr.scored(trial("m", ["high_memory_usage"], "high_memory_usage", contaminated=True))
    assert not rr.scored(trial("m", ["high_memory_usage", "x"], "high_memory_usage", mode="pair"))


def test_report_headline_and_definition(tmp_path):
    ts = [trial("memory_pressure", ["high_memory_usage"], "high_memory_usage"),
          trial("memory_pressure", ["high_memory_usage"], "high_memory_usage"),
          trial("memory_pressure", ["high_memory_usage"], "db_connection_pool_exhaustion"),
          trial("db_outage", [], None),
          trial("db_outage", [], "high_memory_usage", effective=False)]            # not effective: not scored
    (tmp_path / "trials.jsonl").write_text("\n".join(json.dumps(t) for t in ts) + "\n", encoding="utf-8")
    text = rr.report([tmp_path])
    assert "REAL AWS END-TO-END ACCURACY = 75.0% (3/4)" in text
    assert "| memory_pressure | 3 | 2 | 0 | 1 | 0 | 66.7% (2/3) |" in text
    assert "never took hold" in text
    assert "Computed as:" in text


def test_precision_recall_count_a_miss_as_a_false_negative():
    ts = [trial("m", ["high_memory_usage"], "high_memory_usage"), trial("m", ["high_memory_usage"], "NONE"),
          trial("o", [], "high_memory_usage")]
    row = [r for r in rr.prf(ts) if r[0] == "high_memory_usage"][0]
    assert row == ["high_memory_usage", "2", "0.50", "0.50", "0.50"]


def test_pair_result_reads_which_faults_were_named():
    def dec(cause, amb):
        return {"rule_prediction": cause, "escalated": cause is None, "playbook": "restart_service.yml" if cause else None,
                "ambiguous_causes": amb, "ambiguity_rule_would": "escalate" if len(amb) > 1 else cause}
    t = {"truth": ["high_memory_usage", "db_connection_pool_exhaustion"],
         "decisions": [dec("high_memory_usage", ["high_memory_usage"]),
                       dec("db_connection_pool_exhaustion", ["high_memory_usage", "db_connection_pool_exhaustion"])]}
    r = rr.pair_result(t)
    assert r["both_identified"] and not r["only_one"] and r["second_unresolved"] == []
    assert r["ambiguity_seen"] and r["rule_on_would_escalate"] == [False, True]
    t["decisions"] = t["decisions"][:1]
    r = rr.pair_result(t)
    assert r["only_one"] and r["second_unresolved"] == ["db_connection_pool_exhaustion"]


INCIDENT = json.dumps({"label": None, "received_at": "2026-01-01T10:01:00+00:00",
                       "payload": {"alerts": [{"status": "firing", "labels": {"alertname": "ContainerMemoryHigh",
                                                                                  "service": "transaction-service"}}]},
                       "response": {"status": "correlated", "root_cause": "high_memory_usage", "confidence": 0.58,
                                    "second_opinion": {"verdict": "agree", "scorer_service": "transaction-service",
                                                       "scorer_fault": "high_memory_usage", "confidence": 0.9}}})


def _mock_cluster(monkeypatch, calls, captured):
    monkeypatch.setattr(rv.time, "sleep", lambda s: None)
    monkeypatch.setattr(rv.lv, "quiet_or_reset", lambda: True)
    monkeypatch.setattr(rv.lv, "put_label", lambda label: calls.append(f"label={label}"))
    monkeypatch.setattr(rv.lv, "firing", lambda: set())
    monkeypatch.setattr(rv.lv, "prom_scalar", lambda *a, **k: 2.0)
    monkeypatch.setattr(rv.lv, "memory_ratio", lambda: 0.95)
    monkeypatch.setattr(rv.lv, "kubectl", lambda *a, **k: "")
    monkeypatch.setattr(rv.lv, "ask_scorer", lambda *a, **k: {"scorer_service": "transaction-service",
                                                              "scorer_fault": "high_memory_usage", "confidence": 0.9})
    monkeypatch.setattr(rv.lv, "captured", captured)


def test_the_driver_starts_stops_and_never_labels_the_incident(monkeypatch):
    calls: list[str] = []
    lines: list[str] = []

    def captured():
        if "start" in calls and not lines:
            lines.append(INCIDENT)
        return list(lines)

    _mock_cluster(monkeypatch, calls, captured)
    fault = rv.Fault("memory_pressure", "txn", "high_memory_usage", lambda: calls.append("start"),
                     lambda: calls.append("stop"), lambda: 0.95, 0.9, "memory")
    t = rv.run_fault_trial(7, [fault], detect_timeout=60, hold=0)
    assert calls == ["label=None", "start", "stop"]                  # engine never told; started once; stopped
    assert t["truth"] == ["high_memory_usage"] and t["fault_effective"] and t["recovered"]
    assert t["decisions"][0]["rule_prediction"] == "high_memory_usage"
    assert t["decisions"][0]["second_opinion"] == "agree"
    assert t["scorer_probes"][0]["scorer_fault"] == "high_memory_usage"


def test_the_fault_is_stopped_even_when_the_trial_blows_up(monkeypatch):
    calls: list[str] = []

    def boom():
        if "start" in calls:
            raise RuntimeError("engine gone")                         # fails only once the real fault is in place
        return []

    _mock_cluster(monkeypatch, calls, boom)
    fault = rv.Fault("db_outage", "db", None, lambda: calls.append("start"), lambda: calls.append("stop"),
                     lambda: 1.0, 1.0, "replicas")
    try:
        rv.run_fault_trial(1, [fault], detect_timeout=5, hold=0)
    except RuntimeError:
        pass
    assert "stop" in calls


def test_pool_exhaustion_lowers_the_request_with_the_limit_and_restores_both(monkeypatch):
    calls: list[tuple] = []

    def fake(*args, **kwargs):
        calls.append(args)
        if "get" in args and "jsonpath={.spec.template.spec.containers[0].resources.limits.cpu}" in args:
            return "200m"
        if "get" in args and "jsonpath={.spec.template.spec.containers[0].resources.requests.cpu}" in args:
            return "50m"
        return ""

    monkeypatch.setattr(rv.lv, "kubectl", fake)
    state: dict = {}
    rv._pool_start(state)
    set_calls = [c for c in calls if "set" in c and "resources" in c]
    assert set_calls[0][-2:] == ("--requests=cpu=5m", "--limits=cpu=10m")        # request below limit: accepted
    rv._pool_stop(state)
    set_calls = [c for c in calls if "set" in c and "resources" in c]
    assert set_calls[-1][-2:] == ("--requests=cpu=50m", "--limits=cpu=200m")     # the originals are put back
    calls.clear()
    rv._pool_stop({})                                                            # start never got that far
    assert not [c for c in calls if "set" in c and "resources" in c]
