"""The gate-mode readiness check: the bar must not be met by a handful of lucky trials."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evaluation"))
import gate_check as gc  # noqa: E402


def make_session(tmp_path, name, n_trials, verdict="agree", unknown=0, probe_true="agree", probe_wrong="disagree"):
    d = tmp_path / name
    d.mkdir()
    trials, records = [], []
    for i in range(1, n_trials + unknown + 1):
        label = "db_disk_full" if i <= n_trials else None
        stamp = f"2026-11-12T01:{i:02d}:00+00:00"
        trials.append({"trial": i, "label": label, "label_set_at": stamp, "first_incident_s": 80.0,
                       "probes": [{"when": w, "truth": {"verdict": probe_true}, "wrong": {"verdict": probe_wrong}}
                                  for w in ("at first incident", "end of hold")] if label else []})
        records.append({"label": label, "received_at": f"2026-11-12T01:{i:02d}:30+00:00", "payload": {"alerts": []},
                        "response": {"root_cause": label, "second_opinion": {"verdict": verdict} if label else None}})
        if label:                                   # a later update of the same incident
            records.append({"label": label, "received_at": f"2026-11-12T01:{i:02d}:40+00:00", "payload": {"alerts": []},
                            "response": {"root_cause": label, "second_opinion": {"verdict": verdict}}})
    (d / "trials.jsonl").write_text("\n".join(json.dumps(t) for t in trials), encoding="utf-8")
    (d / "incidents.jsonl").write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
    return d


def test_wilson_bound_shrinks_with_more_decisions():
    assert gc.wilson_upper(0, 11) > 0.2           # 11 clean decisions prove little
    assert gc.wilson_upper(0, 40) <= 0.10        # 40 do
    assert gc.wilson_upper(0, 0) == 1.0


def test_three_clean_sessions_meet_the_bar(tmp_path):
    dirs = [make_session(tmp_path, f"s{i}", 8, unknown=2) for i in range(3)]
    rows = gc.evaluate(gc.gather(dirs))
    assert all(ok for _, ok, _ in rows), [r for r in rows if not r[1]]


def test_one_session_is_not_enough_however_clean(tmp_path):
    rows = gc.evaluate(gc.gather([make_session(tmp_path, "s0", 8, unknown=2)]))
    unmet = [name for name, ok, _ in rows if not ok]
    assert any("sessions" in n for n in unmet) and any("decisions >=" in n for n in unmet)


def test_a_single_veto_of_a_correct_decision_fails_the_bar(tmp_path):
    dirs = [make_session(tmp_path, f"s{i}", 8, unknown=2) for i in range(3)]
    path = dirs[1] / "incidents.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[0])
    record["response"]["second_opinion"]["verdict"] = "disagree"
    lines[0] = json.dumps(record)
    path.write_text("\n".join(lines), encoding="utf-8")
    rows = dict((n, ok) for n, ok, _ in gc.evaluate(gc.gather(dirs)))
    assert not rows[f"vetoes of correct decisions <= {gc.MAX_VETOES}"]


def test_probes_that_miss_a_wrong_claim_fail_the_bar(tmp_path):
    dirs = [make_session(tmp_path, f"s{i}", 8, unknown=2, probe_wrong="agree") for i in range(3)]
    rows = dict((n, ok) for n, ok, _ in gc.evaluate(gc.gather(dirs)))
    assert not rows[f"wrong claims caught >= {gc.MIN_PROBE_RATE:.0%} of >= {gc.MIN_PROBES} probes"]


def test_main_exits_nonzero_when_not_ready(tmp_path, monkeypatch, capsys):
    d = make_session(tmp_path, "s0", 4)
    monkeypatch.setattr(sys, "argv", ["gate_check", str(d)])
    assert gc.main() == 1
    assert "NOT READY" in capsys.readouterr().out
