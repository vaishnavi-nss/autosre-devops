"""ambiguity_report.py: the replay of the rule over recorded incidents."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evaluation"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ambiguity_report as ar    # noqa: E402

DISK = [("DBDiskNearFull", "database"), ("ErrorRateHigh", "transaction-service")]
MEMORY = [("ContainerMemoryHigh", "transaction-service")]


def rec(at, label, alerts, response=None):
    return {"label": label, "received_at": at, "response": response or {},
            "payload": {"alerts": [{"status": "firing", "labels": {"alertname": n, "service": s}} for n, s in alerts]}}


def write(d: Path, trials, records):
    d.mkdir(exist_ok=True)
    (d / "trials.jsonl").write_text("\n".join(json.dumps(t) for t in trials) + "\n", encoding="utf-8")
    (d / "incidents.jsonl").write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


def test_the_rule_escalates_two_real_deciding_symptoms_but_not_one():
    both = rec("2026-01-01T10:01:00+00:00", None, DISK + MEMORY)
    one = rec("2026-01-01T10:01:00+00:00", None, DISK)
    assert ar.decide(both, False) == "db_disk_full" and ar.decide(both, True) is None
    assert ar.decide(one, False) == "db_disk_full" and ar.decide(one, True) == "db_disk_full"


def test_cost_counts_a_correct_action_the_rule_would_turn_into_an_escalation(tmp_path):
    t = {"trial": 1, "label": "db_disk_full", "contaminated": False, "label_set_at": "2026-01-01T10:00:00+00:00"}
    write(tmp_path, [t], [rec("2026-01-01T10:01:00+00:00", "db_disk_full", DISK),
                          rec("2026-01-01T10:02:00+00:00", "db_disk_full", DISK + MEMORY)])
    text = ar.cost([tmp_path])
    assert "Changed by the rule: 1 x correct -> missed" in text
    assert "First webhook of each trial** (1 incidents)" in text and "nothing" in text.split("First webhook")[1]


def test_pairs_reports_what_was_live_what_the_rule_would_do_and_the_probes(tmp_path):
    trial = {"trial": 1, "label": "db_disk_full + high_memory_usage", "mode": "pair", "contaminated": False,
             "pair": ["db_disk_full", "high_memory_usage"], "label_set_at": "2026-01-01T10:00:00+00:00",
             "reset_at": "2026-01-01T10:10:00+00:00",
             "probes": [{"when": "at first incident",
                         "truth": {"db_disk_full": {"verdict": "agree"}, "high_memory_usage": {"verdict": "no_opinion"}},
                         "wrong": {"verdict": "disagree"}}]}
    acted = {"status": "correlated", "root_cause": "db_disk_full", "second_opinion": {"verdict": "agree"}}
    vetoed = {"status": "vetoed", "root_cause": None, "rule_root_cause": "db_disk_full",
              "second_opinion": {"verdict": "disagree"}}
    write(tmp_path, [trial], [rec("2026-01-01T10:02:00+00:00", None, DISK + MEMORY, acted),
                              rec("2026-01-01T10:04:00+00:00", None, DISK + MEMORY, vetoed)])
    text = ar.pairs([tmp_path])
    assert "| 1 | db_disk_full + high_memory_usage | 1 | db_disk_full | agree | db_disk_full | escalate |" in text
    assert "withheld (db_disk_full)" in text
    assert "| live: acted, cause in pair | 1 |" in text and "| live: gate withheld | 1 |" in text
    assert "| on escalates | 2 |" in text
    assert "true cause high_memory_usage: no_opinion" in text and "absent cause: disagree" in text


def test_wrong_lists_webhooks_naming_a_cause_other_than_the_label(tmp_path):
    t = {"trial": 1, "label": "high_memory_usage", "contaminated": False, "label_set_at": "2026-01-01T10:00:00+00:00"}
    write(tmp_path, [t], [rec("2026-01-01T10:01:00+00:00", "high_memory_usage", MEMORY),
                          rec("2026-01-01T10:02:00+00:00", "high_memory_usage", DISK)])
    text = ar.wrong([tmp_path])
    assert "| 1 | high_memory_usage | db_disk_full | webhook 2 | DBDiskNearFull, ErrorRateHigh |" in text
    assert text.count("| high_memory_usage | db_disk_full |") == 1


def test_leaks_separates_a_leftover_alert_from_one_that_arose_during_the_trial(tmp_path):
    def alert_rec(at, label, name, svc, starts):
        r = rec(at, label, [(name, svc)])
        r["payload"]["alerts"][0]["startsAt"] = starts
        return r

    t1 = {"trial": 1, "label": "db_disk_full", "contaminated": False, "label_set_at": "2026-01-01T10:00:00+00:00",
          "injected_at": "2026-01-01T10:00:05+00:00"}
    t2 = {"trial": 2, "label": "db_disk_full", "contaminated": False, "label_set_at": "2026-01-01T10:20:00+00:00",
          "injected_at": "2026-01-01T10:20:05+00:00"}
    t3 = {"trial": 3, "label": "db_disk_full", "contaminated": False, "label_set_at": "2026-01-01T10:40:00+00:00",
          "injected_at": "2026-01-01T10:40:05+00:00"}
    t0 = {"trial": 0, "label": "high_memory_usage", "contaminated": True, "label_set_at": "2026-01-01T09:40:00+00:00",
          "injected_at": "2026-01-01T09:40:05+00:00"}
    recs = [alert_rec("2026-01-01T10:22:00+00:00", "db_disk_full", "ContainerMemoryHigh", "transaction-service",
                      "2026-01-01T10:10:00Z"),          # trial 2: memory alert began before its injection: leftover
            alert_rec("2026-01-01T10:42:00+00:00", "db_disk_full", "ContainerMemoryHigh", "transaction-service",
                      "2026-01-01T10:41:00Z"),          # trial 3: began after its injection: arose during it
            rec("2026-01-01T10:02:00+00:00", "db_disk_full", DISK)]   # trial 1: correct, ignored
    write(tmp_path, [t0, t1, t2, t3], recs)
    text = ar.leaks([tmp_path])
    assert "| before this trial's injection (leftover) | same cause as the previous trial | 1 |" not in text
    assert "| before this trial's injection (leftover) | not the previous trial's cause | 1 |" in text
    assert "| after it (arose during the trial) | not the previous trial's cause | 1 |" in text
    assert "| 2 | db_disk_full | high_memory_usage | 1 | before this trial's injection (leftover) | 605 |" in text


def test_a_contaminated_trials_records_are_not_credited_to_the_trial_before_it(tmp_path):
    ok = {"trial": 1, "label": "db_disk_full", "contaminated": False, "label_set_at": "2026-01-01T10:00:00+00:00",
          "injected_at": "2026-01-01T10:00:05+00:00"}
    bad = {"trial": 2, "label": "high_memory_usage", "contaminated": True, "label_set_at": "2026-01-01T10:20:00+00:00",
           "injected_at": "2026-01-01T10:20:05+00:00"}
    write(tmp_path, [ok, bad], [rec("2026-01-01T10:02:00+00:00", "db_disk_full", DISK),
                                rec("2026-01-01T10:22:00+00:00", "high_memory_usage", MEMORY)])
    assert "| 1 | high_memory_usage |" not in ar.wrong([tmp_path])
    assert "(1 incidents)" in ar.cost([tmp_path]).split("First webhook")[0]
