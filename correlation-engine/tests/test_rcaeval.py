"""Tests for the RCAEval replay baseline and its feature extraction, on synthetic data."""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evaluation"))

import rcaeval_eval as ev          # noqa: E402
import rcaeval_features as rf      # noqa: E402


def frame(shifted: dict, n=200, inject=100, seed=0):
    """Flat noisy metrics; the columns in `shifted` jump by the given factor after `inject`."""
    rng = np.random.default_rng(seed)
    cols = {"time": np.arange(n)}
    for name in ("a_cpu", "a_mem", "a_latency", "b_cpu", "b_mem", "b_latency", "b_error"):
        base = 10 + rng.normal(0, 0.5, n)
        base[inject:] *= shifted.get(name, 1.0)
        cols[name] = base
    return pd.DataFrame(cols)


def test_parse_column_folds_suffixes_and_skips_hosts():
    assert rf.parse_column("cartservice_latency-90") == ("cartservice", "latency")
    assert rf.parse_column("ts-auth-service_workload") == ("ts-auth-service", "workload")
    assert rf.parse_column("main_cpu") is None
    assert rf.parse_column("a_unknownkind") is None


def test_a_shifted_metric_scores_high_and_a_flat_one_near_zero():
    scores = rf.service_scores(frame({"a_cpu": 5.0}), inject_time=100, window_s=60)
    assert scores["a"]["cpu"] > 20
    assert abs(scores["b"]["cpu"]) < 3


def test_scores_do_not_all_tie_at_a_ceiling():
    big = rf.service_scores(frame({"a_cpu": 50.0, "b_cpu": 5.0}), 100, 60)
    assert big["a"]["cpu"] > big["b"]["cpu"] * 2


def test_rules_name_the_service_with_the_resource_symptom():
    scores = rf.service_scores(frame({"a_cpu": 6.0, "b_latency": 3.0}), 100, 60)
    assert ev.rule_diagnose(scores, 8.0) == ("a", "cpu")


def test_rules_call_latency_alone_delay_and_with_errors_loss():
    delay = rf.service_scores(frame({"a_latency": 6.0}), 100, 60)
    assert ev.rule_diagnose(delay, 8.0) == ("a", "delay")
    loss = frame({"a_latency": 6.0}).assign(a_error=lambda d: 1.0)
    loss.loc[100:, "a_error"] = 9.0
    assert ev.rule_diagnose(rf.service_scores(loss, 100, 60), 8.0) == ("a", "loss")


def test_rules_escalate_when_nothing_fires():
    quiet = rf.service_scores(frame({}), 100, 60)
    assert ev.rule_diagnose(quiet, 8.0) is None


def test_outcome_scores_acting_on_an_unknown_fault_as_wrong_and_escalating_as_correct():
    assert ev.outcome("a", "disk", ("a", "cpu"), in_vocabulary=False) == "wrong action"
    assert ev.outcome("a", "disk", None, in_vocabulary=False) == "correct escalation"
    assert ev.outcome("a", "cpu", None, in_vocabulary=True) == "missed"
    assert ev.outcome("a", "cpu", ("b", "cpu")) == "wrong action"


def test_calibration_error_is_zero_when_confidence_equals_accuracy():
    assert ev.ece([0.5] * 10, [1, 0] * 5) < 1e-9
    assert ev.ece([0.9] * 10, [0] * 10) > 0.8
