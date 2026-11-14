"""The two-fault mode of the live harness, and that it never reaches the gate check."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evaluation"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import gate_check as gc          # noqa: E402
import live_validate as lv       # noqa: E402


def _run_main(monkeypatch, tmp_path, *extra):
    calls: list[tuple] = []
    ran: list = []
    monkeypatch.setattr(lv, "prepare", lambda cpu_limit=None, engine_env=None: calls.append(("prepare", engine_env)))
    monkeypatch.setattr(lv, "reset_cluster", lambda: None)
    monkeypatch.setattr(lv, "captured", lambda: [])
    monkeypatch.setattr(lv, "kubectl", lambda *a, **k: calls.append(("kubectl", a)) or "")
    monkeypatch.setattr(lv, "run_pair_trial",
                        lambda i, pair, rng, d, h: ran.append(pair) or
                        {"trial": i, "label": " + ".join(pair), "mode": "pair", "contaminated": False})
    monkeypatch.setattr(sys, "argv", ["live_validate", "--out", str(tmp_path), "--per-cause", "0", "--unknown", "0",
                                      *extra])
    lv.main()
    return ran, calls


def test_pairs_are_planned_per_pair_and_conditions_are_recorded(monkeypatch, tmp_path):
    ran, _ = _run_main(monkeypatch, tmp_path, "--pairs", "2", "--cpu-limit", "1000m")
    assert len(ran) == 6 and set(ran) == set(lv.PAIRS)
    assert all(isinstance(p, tuple) and len(p) == 2 and p[0] != p[1] for p in ran)
    cond = json.loads((tmp_path / "conditions.json").read_text(encoding="utf-8"))
    assert cond["cpu_limit"] == "1000m" and cond["pairs"] == 2


def test_engine_settings_are_applied_and_always_put_back(monkeypatch, tmp_path):
    _, calls = _run_main(monkeypatch, tmp_path, "--pairs", "1", "--engine-env", "SECOND_OPINION_MODE=gate",
                         "--engine-env", "ESCALATE_ON_AMBIGUITY=true")
    assert ("prepare", ["SECOND_OPINION_MODE=gate", "ESCALATE_ON_AMBIGUITY=true"]) in calls
    restore = [a for kind, a in calls if kind == "kubectl"][-1]
    assert "SECOND_OPINION_MODE=shadow" in restore and "ESCALATE_ON_AMBIGUITY-" in restore


def test_no_restore_when_no_engine_settings_were_asked_for(monkeypatch, tmp_path):
    _, calls = _run_main(monkeypatch, tmp_path, "--pairs", "1")
    assert not [c for c in calls if c[0] == "kubectl"]


def test_gate_check_ignores_pair_trials(tmp_path):
    trial = {"trial": 1, "label": "db_disk_full + high_memory_usage", "mode": "pair", "contaminated": False,
             "pair": ["db_disk_full", "high_memory_usage"], "label_set_at": "2026-01-01T10:00:00+00:00",
             "first_incident_s": 90.0,
             "probes": [{"when": "at first incident", "truth": {"db_disk_full": {"verdict": "agree"}},
                         "wrong": {"verdict": "disagree"}}]}
    (tmp_path / "trials.jsonl").write_text(json.dumps(trial) + "\n", encoding="utf-8")
    (tmp_path / "incidents.jsonl").write_text("", encoding="utf-8")
    data = gc.gather([tmp_path])
    assert data["trials"] == 0 and not data["probes_true"] and not data["probes_wrong"]
