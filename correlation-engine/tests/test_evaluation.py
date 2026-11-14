"""Tests for the evaluation tooling and the incident-capture endpoint.

The evaluation code produces the numbers that go in the report, so it gets the
same scrutiny as the engine: a wrong metric is worse than no metric.
"""

import json
import sys
from pathlib import Path

import pytest

ENGINE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE_DIR / "evaluation"))

import metrics as m          # noqa: E402
import simulate              # noqa: E402
from evaluate import (       # noqa: E402
    attribute_failures,
    correlate_with_fallback,
    load_replay,
    run_engine,
)
from signatures import SIGNATURES   # noqa: E402


# -- the simulator ----------------------------------------------------------------

def test_simulation_is_deterministic_for_a_seed():
    a = simulate.generate(200, simulate.PRESETS["hard"], seed=5)
    b = simulate.generate(200, simulate.PRESETS["hard"], seed=5)
    c = simulate.generate(200, simulate.PRESETS["hard"], seed=6)
    assert a == b
    assert a != c


def test_simulation_yields_exactly_n_nonempty_incidents():
    data = simulate.generate(300, simulate.PRESETS["hard"], seed=1)
    assert len(data) == 300
    assert all(r["payload"]["alerts"] for r in data)


def test_simulated_labels_are_known_causes_or_none():
    labels = {r["label"] for r in simulate.generate(500, simulate.PRESETS["moderate"], seed=2)}
    assert labels <= set(SIGNATURES) | {None}
    assert labels == set(SIGNATURES) | {None}        # every class is represented


def test_deep_fault_incidents_are_labelled_unknown():
    data = simulate.generate(2000, simulate.PRESETS["moderate"], seed=3)
    deep = [r for r in data if r["meta"]["kind"] == "deep_fault"]
    assert deep, "expected some deep_fault incidents"
    assert all(r["label"] is None for r in deep)


def test_clean_preset_is_solved_perfectly_by_the_engine():
    # Regression guard: with every symptom present and no noise, the engine must
    # be right every time. A failure here means the engine and simulator diverged.
    data = simulate.generate(1000, simulate.PRESETS["clean"], seed=4)
    truths, preds, _, _ = run_engine(data)
    assert m.accuracy(truths, preds) == 1.0


def test_failures_in_noisy_data_are_always_explained():
    # attribute_failures labels anything it cannot account for as "unexplained".
    # That bucket must stay empty, or the simulator and engine disagree about
    # something and the reported numbers cannot be trusted.
    data = simulate.generate(3000, simulate.PRESETS["hard"], seed=9)
    truths, preds, _, _ = run_engine(data)
    assert "unexplained" not in attribute_failures(data, truths, preds)


def test_fallback_variant_is_worse_on_deep_faults():
    data = [
        r for r in simulate.generate(3000, simulate.PRESETS["clean"], seed=8)
        if r["meta"]["kind"] == "deep_fault"
    ]
    strict_t, strict_p, _, _ = run_engine(data)
    fall_t, fall_p, _, _ = run_engine(data, correlate_with_fallback)
    assert m.outcome_counts(strict_t, strict_p)["wrong_action"] == 0
    assert m.outcome_counts(fall_t, fall_p)["wrong_action"] > 0


# -- metrics -----------------------------------------------------------------------

CAUSE_A, CAUSE_B = "db_disk_full", "high_memory_usage"


@pytest.mark.parametrize(
    "truth, pred, expected",
    [
        (CAUSE_A, CAUSE_A, "correct_action"),
        (m.ESCALATE, m.ESCALATE, "correct_escalation"),
        (CAUSE_A, m.ESCALATE, "missed"),
        (CAUSE_A, CAUSE_B, "wrong_action"),
        (m.ESCALATE, CAUSE_A, "wrong_action"),   # acting when there was nothing to act on
    ],
)
def test_outcome_classification(truth, pred, expected):
    assert m.outcome(truth, pred) == expected


def test_norm_maps_none_to_escalate():
    assert m.norm(None) == m.ESCALATE
    assert m.norm(CAUSE_A) == CAUSE_A


def test_per_class_precision_and_recall():
    truths = [CAUSE_A, CAUSE_A, CAUSE_A, CAUSE_B]
    preds = [CAUSE_A, CAUSE_A, CAUSE_B, CAUSE_B]
    scores = m.per_class(truths, preds)
    assert scores[CAUSE_A]["precision"] == 1.0
    assert scores[CAUSE_A]["recall"] == pytest.approx(2 / 3)
    assert scores[CAUSE_B]["precision"] == 0.5
    assert scores[CAUSE_B]["recall"] == 1.0


def test_gate_table_escalates_low_confidence_actions():
    truths = [CAUSE_A, CAUSE_A, CAUSE_B]
    preds = [CAUSE_A, CAUSE_B, CAUSE_B]          # second one is a wrong action
    confs = [0.9, 0.5, 0.9]
    open_gate, strict_gate = m.gate_table(truths, preds, confs, gates=(0.0, 0.75))
    assert open_gate["actions"] == 3 and open_gate["wrong_actions"] == 1
    # At 0.75 the low-confidence wrong action is escalated instead of executed.
    assert strict_gate["actions"] == 2 and strict_gate["wrong_actions"] == 0
    assert strict_gate["precision"] == 1.0


def test_gate_table_handles_no_actions():
    row = m.gate_table([CAUSE_A], [m.ESCALATE], [0.0], gates=(0.5,))[0]
    assert row["actions"] == 0 and row["precision"] == 0.0


# -- replaying real captured incidents ---------------------------------------------

def test_replay_round_trip(tmp_path, sample_payload):
    capture = tmp_path / "captured.jsonl"
    capture.write_text(
        json.dumps({"label": "db_connection_pool_exhaustion", "payload": sample_payload}) + "\n"
        + "\n"                                                     # blank lines are skipped
        + json.dumps({"label": None, "payload": {"alerts": []}}) + "\n",
        encoding="utf-8",
    )
    records = load_replay(capture)
    truths, preds, confs, _ = run_engine(records)
    assert truths == ["db_connection_pool_exhaustion", m.ESCALATE]
    assert preds == ["db_connection_pool_exhaustion", m.ESCALATE]
    assert confs == [1.0, 0.0]


def test_replay_rejects_records_without_a_payload(tmp_path):
    bad = tmp_path / "bad.jsonl"
    bad.write_text(json.dumps({"label": None}) + "\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        load_replay(bad)


# -- the capture endpoint ----------------------------------------------------------

@pytest.fixture
def capturing(monkeypatch, tmp_path):
    import app as engine_app

    path = tmp_path / "incidents.jsonl"
    monkeypatch.setattr(engine_app, "CAPTURE_PATH", str(path))
    monkeypatch.setattr(engine_app, "_capture_label", None)
    return path


def test_capture_is_off_by_default(client):
    assert client.put("/capture/label", json={"label": None}).status_code == 404


def test_capture_records_incident_with_its_label(client, capturing, sample_payload):
    assert client.put("/capture/label", json={"label": "db_connection_pool_exhaustion"}).status_code == 200
    client.post("/alerts", json=sample_payload)

    lines = capturing.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["label"] == "db_connection_pool_exhaustion"
    assert record["payload"]["groupKey"] == sample_payload["groupKey"]
    assert "received_at" in record


def test_captured_file_can_be_replayed_by_the_evaluator(client, capturing, sample_payload):
    client.put("/capture/label", json={"label": "db_connection_pool_exhaustion"})
    client.post("/alerts", json=sample_payload)
    truths, preds, _, _ = run_engine(load_replay(capturing))
    assert truths == preds == ["db_connection_pool_exhaustion"]


def test_resolved_notifications_are_not_captured_as_incidents(client, capturing, sample_payload):
    # When alerts clear, Alertmanager sends a "resolved" notification. Capturing
    # it would score as a missed detection in the evaluation.
    client.put("/capture/label", json={"label": "db_connection_pool_exhaustion"})
    for alert in sample_payload["alerts"]:
        alert["status"] = "resolved"
    sample_payload["status"] = "resolved"
    assert client.post("/alerts", json=sample_payload).status_code == 202
    assert not capturing.exists() or capturing.read_text(encoding="utf-8") == ""


def test_capture_rejects_an_unknown_label(client, capturing):
    response = client.put("/capture/label", json={"label": "cosmic_rays"})
    assert response.status_code == 422
    assert "db_disk_full" in response.json()["detail"]


def test_capture_label_none_means_escalate(client, capturing, sample_payload):
    client.put("/capture/label", json={"label": None})
    client.post("/alerts", json=sample_payload)
    assert json.loads(capturing.read_text(encoding="utf-8"))["label"] is None
