"""Tests for evaluation/live_report.py, using synthetic trial data.

The report decides which incidents count and how, so a bug here would quietly
change the headline numbers - worth checking before spending cloud time on it.
"""

import json
import sys
from pathlib import Path

ENGINE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE_DIR / "evaluation"))

import live_report as lr    # noqa: E402

POOL_ALERTS = [
    ("DBPoolNearExhaustion", "transaction-service"),
    ("ErrorRateHigh", "transaction-service"),
]


def payload(pairs, status="firing"):
    return {"alerts": [
        {"status": status, "labels": {"alertname": n, "service": s}} for n, s in pairs
    ]}


def record(at, label, pairs):
    return {"label": label, "received_at": at, "payload": payload(pairs)}


def trial(n, label, label_set, injected, **extra):
    return {"trial": n, "label": label, "contaminated": False,
            "label_set_at": label_set, "injected_at": injected, **extra}


def write(tmp_path, trials, records):
    (tmp_path / "trials.jsonl").write_text("\n".join(json.dumps(t) for t in trials) + "\n", encoding="utf-8")
    (tmp_path / "incidents.jsonl").write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


def test_records_are_assigned_to_the_trial_labelled_before_them():
    trials = [trial(1, "db_disk_full", "2026-01-01T10:00:00+00:00", "2026-01-01T10:00:05+00:00"),
              trial(2, None, "2026-01-01T10:20:00+00:00", "2026-01-01T10:20:05+00:00")]
    recs = [record("2026-01-01T10:02:00+00:00", "db_disk_full", [("DBDiskNearFull", "database")]),
            record("2026-01-01T10:22:00+00:00", None, [("APILatencyHigh", "api-gateway")])]
    grouped = lr.assign(recs, trials)
    assert [r["received_at"] for r in grouped[1]] == ["2026-01-01T10:02:00+00:00"]
    assert [r["received_at"] for r in grouped[2]] == ["2026-01-01T10:22:00+00:00"]


def test_first_decision_uses_the_first_webhook_not_the_last(tmp_path):
    # The first webhook has only the deciding symptom (conf 0.55); the later one
    # has more. The dispatcher acts on the first, so that is what is scored.
    trials = [trial(1, "db_connection_pool_exhaustion", "2026-01-01T10:00:00+00:00", "2026-01-01T10:00:10+00:00")]
    recs = [record("2026-01-01T10:01:00+00:00", "db_connection_pool_exhaustion", POOL_ALERTS[:1]),
            record("2026-01-01T10:02:30+00:00", "db_connection_pool_exhaustion", POOL_ALERTS)]
    write(tmp_path, trials, recs)
    text = lr.build(tmp_path)
    assert "| 1 | db_connection_pool_exhaustion | DBPoolNearExhaustion | db_connection_pool_exhaustion | 0.55 | 50s | 2 |" in text
    assert "(0.75)" in text                      # the later, fuller decision


def test_a_gated_dispatcher_acts_on_the_first_webhook_that_clears_the_gate(tmp_path):
    # The first webhook (deciding symptom only, 0.55) is below the 0.75 gate; the
    # second (0.75) clears it. Detection is at 50s, but acting is at 140s.
    trials = [trial(1, "db_connection_pool_exhaustion", "2026-01-01T10:00:00+00:00", "2026-01-01T10:00:10+00:00")]
    recs = [record("2026-01-01T10:01:00+00:00", "db_connection_pool_exhaustion", POOL_ALERTS[:1]),
            record("2026-01-01T10:02:30+00:00", "db_connection_pool_exhaustion", POOL_ALERTS)]
    write(tmp_path, trials, recs)
    text = lr.build(tmp_path)
    assert "| 140s | " in text                                  # the "acts at 0.75" cell
    assert "first incident reaches the engine: median **50s**" in text
    assert "would act on: median **140s**" in text
    assert "in 1 of 1 trials" in text


def test_a_trial_that_never_reaches_the_gate_is_called_out(tmp_path):
    trials = [trial(1, "db_connection_pool_exhaustion", "2026-01-01T10:00:00+00:00", "2026-01-01T10:00:10+00:00")]
    recs = [record("2026-01-01T10:01:00+00:00", "db_connection_pool_exhaustion", POOL_ALERTS[:1])]
    write(tmp_path, trials, recs)
    text = lr.build(tmp_path)
    assert "| never | " in text
    assert "never reached the gate** in trials #1" in text


def test_acting_on_the_wrong_cause_is_marked(tmp_path):
    # Labelled memory pressure, but the full pool symptom set clears the gate.
    trials = [trial(1, "high_memory_usage", "2026-01-01T10:00:00+00:00", "2026-01-01T10:00:10+00:00")]
    recs = [record("2026-01-01T10:01:00+00:00", "high_memory_usage",
                   POOL_ALERTS + [("PodRestartsHigh", "transaction-service")])]
    write(tmp_path, trials, recs)
    assert "(WRONG)" in lr.build(tmp_path)


def test_silent_trials_are_reported_but_not_scored_as_engine_failures(tmp_path):
    trials = [trial(1, "db_disk_full", "2026-01-01T10:00:00+00:00", "2026-01-01T10:00:10+00:00"),
              trial(2, "db_connection_pool_exhaustion", "2026-01-01T10:20:00+00:00", "2026-01-01T10:20:10+00:00")]
    recs = [record("2026-01-01T10:02:00+00:00", "db_disk_full", [("DBDiskNearFull", "database")])]
    write(tmp_path, trials, recs)
    text = lr.build(tmp_path)
    assert "an incident reached the engine: **1 of 2**" in text
    assert "no incident delivered" in text and "#2 (db_connection_pool_exhaustion)" in text
    assert "**1 incidents.**" in text            # only the delivered trial is scored


def test_contaminated_trials_are_excluded(tmp_path):
    trials = [trial(1, "db_disk_full", "2026-01-01T10:00:00+00:00", "2026-01-01T10:00:10+00:00"),
              {**trial(2, "db_disk_full", "2026-01-01T10:20:00+00:00", "2026-01-01T10:20:10+00:00"),
               "contaminated": True}]
    recs = [record("2026-01-01T10:02:00+00:00", "db_disk_full", [("DBDiskNearFull", "database")]),
            record("2026-01-01T10:22:00+00:00", "db_disk_full", [("DBDiskNearFull", "database")])]
    write(tmp_path, trials, recs)
    text = lr.build(tmp_path)
    assert "excluded" in text and "#2" in text
    assert "usable: **1**" in text


def test_an_unknown_fault_escalating_is_a_correct_outcome(tmp_path):
    trials = [trial(1, None, "2026-01-01T10:00:00+00:00", "2026-01-01T10:00:10+00:00")]
    recs = [record("2026-01-01T10:01:00+00:00", None, [("APILatencyHigh", "api-gateway")])]
    write(tmp_path, trials, recs)
    text = lr.build(tmp_path)
    assert "correct escalation" in text


def test_a_wrong_decision_is_flagged_as_a_wrong_action(tmp_path):
    # Labelled memory pressure, but the engine sees a pool symptom.
    trials = [trial(1, "high_memory_usage", "2026-01-01T10:00:00+00:00", "2026-01-01T10:00:10+00:00")]
    recs = [record("2026-01-01T10:01:00+00:00", "high_memory_usage", POOL_ALERTS)]
    write(tmp_path, trials, recs)
    assert "wrong action" in lr.build(tmp_path)


def test_live_validate_label_request_is_valid_python():
    # live_validate builds a python one-liner to run inside the engine pod; a quoting
    # mistake there would only surface on the cluster, so compile it here.
    import live_validate as lv
    calls = []
    lv.kubectl = lambda *args, **kw: calls.append(args) or ""
    for label in ("db_disk_full", None):
        lv.put_label(label)
    for call in calls:
        compile(call[-1], "<pod-code>", "exec")
    assert '"label": null' in calls[1][-1]


def test_injection_goes_through_the_api_server_proxy_not_exec():
    # Regression: injecting memory pressure via `kubectl exec` ran the client inside the
    # container's own memory cgroup, so the injector's overhead pushed the pod past its
    # limit and it was OOM-killed (exit 137). Nothing may run inside the target pod.
    import live_validate as lv
    calls = []
    lv.kubectl = lambda *args, **kw: calls.append((args, kw)) or ""

    lv.pod_http("transaction-service", "POST", "/chaos/memory?mb=76")
    lv.pod_http("transaction-service", "GET", "/chaos")
    lv.pod_http("api-gateway", "PUT", "/x")

    (post_args, post_kw), (get_args, _), (put_args, _) = calls
    assert post_args[0] == "create" and post_args[1] == "--raw"
    assert post_args[2] == "/api/v1/namespaces/autosre/services/transaction-service:http/proxy/chaos/memory?mb=76"
    assert post_kw["stdin"] == ""
    assert get_args[:2] == ("get", "--raw")
    assert put_args[0] == "replace"
    assert not any("exec" in a for args, _ in calls for a in args)


def test_resume_runs_only_what_is_missing(tmp_path, monkeypatch):
    # An errored trial does not count as done, so it is redone; the numbering continues.
    import live_validate as lv

    trials = [
        {"trial": 1, "label": None, "contaminated": False},
        {"trial": 2, "label": "db_connection_pool_exhaustion", "contaminated": False},
        {"trial": 3, "label": "high_memory_usage", "error": "OOM", "contaminated": True},
    ]
    (tmp_path / "trials.jsonl").write_text("\n".join(json.dumps(t) for t in trials) + "\n", encoding="utf-8")

    ran = []
    monkeypatch.setattr(lv, "prepare", lambda: None)
    monkeypatch.setattr(lv, "reset_cluster", lambda: None)
    monkeypatch.setattr(lv, "captured", lambda: [])
    monkeypatch.setattr(lv, "run_trial",
                        lambda i, label, rng, d, h: ran.append((i, label)) or
                        {"trial": i, "label": label, "contaminated": False})
    monkeypatch.setattr(sys, "argv", ["live_validate", "--out", str(tmp_path), "--resume",
                                      "--per-cause", "2", "--unknown", "1"])
    lv.main()

    labels = [label for _, label in ran]
    assert labels.count("db_connection_pool_exhaustion") == 1       # 2 wanted, 1 done
    assert labels.count("db_disk_full") == 2                          # none done
    assert labels.count("high_memory_usage") == 2                     # the errored one is redone
    assert labels.count(None) == 0                                    # 1 wanted, 1 done
    assert sorted(i for i, _ in ran) == [4, 5, 6, 7, 8]               # numbering continues from 3


def so_record(at, label, pairs, verdict, reason="r"):
    rec = record(at, label, pairs)
    rec["response"] = {"second_opinion": {"verdict": verdict, "reason": reason}}
    return rec


def test_second_opinion_section_scores_verdicts_and_probes(tmp_path):
    trials = [trial(1, "db_connection_pool_exhaustion", "2026-01-01T10:00:00+00:00", "2026-01-01T10:00:10+00:00",
                    probes=[{"when": "at first incident", "truth": {"verdict": "agree"}, "wrong": {"verdict": "disagree"}},
                            {"when": "end of hold", "truth": {"verdict": "no_opinion"}, "wrong": {"verdict": "agree"}}]),
              trial(2, "db_disk_full", "2026-01-01T10:20:00+00:00", "2026-01-01T10:20:10+00:00")]
    recs = [so_record("2026-01-01T10:01:00+00:00", "db_connection_pool_exhaustion", POOL_ALERTS, "agree"),
            so_record("2026-01-01T10:21:30+00:00", "db_disk_full", [("DBDiskNearFull", "database")], "disagree",
                      "metrics point at frontend")]
    write(tmp_path, trials, recs)
    text = lr.build(tmp_path)
    assert "## Second opinion" in text
    assert "on the **2** correct decisions the scorer said: agree 1, disagree 1" in text
    assert "(**1 of 2**)" in text                                   # one false veto in gate mode
    assert "**wrong diagnosis** (2 probes): agree 1, disagree 1. Caught (disagreed): **1 of 2**" in text
    assert "**true diagnosis** (2 probes): agree 1, no_opinion 1" in text


def test_no_second_opinion_section_when_the_run_had_none(tmp_path):
    trials = [trial(1, "db_disk_full", "2026-01-01T10:00:00+00:00", "2026-01-01T10:00:10+00:00")]
    write(tmp_path, trials, [record("2026-01-01T10:02:00+00:00", "db_disk_full", [("DBDiskNearFull", "database")])])
    assert "Second opinion" not in lr.build(tmp_path)


def test_the_scorer_probe_pairs_a_true_and_a_wrong_diagnosis_for_every_label(monkeypatch):
    import live_validate as lv
    calls = []
    monkeypatch.setattr(lv, "ask_scorer", lambda cause, service, start: calls.append((cause, service, start)) or {"verdict": "agree"})
    for label in ("db_connection_pool_exhaustion", "high_memory_usage", "db_disk_full"):
        probe = lv.probe_scorer(label, 1000.0, "now")
        assert probe["truth"] and probe["wrong"]
        assert lv.TRUTH[label] != lv.WRONG[label]
    gateway = lv.probe_scorer(None, 1000.0, "now")
    assert "truth" not in gateway and gateway["wrong"]
    assert all(start == 1060.0 for _, _, start in calls)            # alerts fire ~60s after the fault


def test_the_scorer_probe_never_raises_when_the_call_fails(monkeypatch):
    import live_validate as lv

    def boom(*a, **k):
        raise RuntimeError("exec failed")
    monkeypatch.setattr(lv, "kubectl", boom)
    reply = lv.ask_scorer("db_disk_full", "database", 1.0)
    assert reply["verdict"] == "unavailable" and "exec failed" in reply["reason"]


def test_a_collect_trial_records_feature_tables_at_each_window_and_resets(monkeypatch):
    import random
    import live_validate as lv
    asked, events = [], []
    monkeypatch.setattr(lv, "ask_features", lambda start: asked.append(start) or {"scores": {"database": {"disk": 9.0}}})
    monkeypatch.setattr(lv, "reset_cluster", lambda: events.append("reset"))
    monkeypatch.setattr(lv.time, "sleep", lambda s: None)
    monkeypatch.setitem(lv.KINDS, "db_disk_full", lambda rng: events.append("inject") or {"ratio": 0.95})
    trial = lv.run_collect_trial(3, "db_disk_full", random.Random(0), settle=0, windows=(150, 270))
    assert events == ["inject", "reset"]
    assert [p["when"] for p in trial["probes"]] == ["150s after injection", "270s after injection"]
    assert all(p["features"]["scores"]["database"]["disk"] == 9.0 for p in trial["probes"])
    assert len(asked) == 2 and not trial["contaminated"] and trial["mode"] == "collect"
