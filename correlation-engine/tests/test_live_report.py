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


def test_pod_http_request_is_valid_python():
    import live_validate as lv
    calls = []
    lv.kubectl = lambda *args, **kw: calls.append(args) or ""
    lv.pod_http("transaction-service", "POST", "/chaos/pool?active=19")
    compile(calls[0][-1], "<pod-code>", "exec")
    assert "active=19" in calls[0][-1]
