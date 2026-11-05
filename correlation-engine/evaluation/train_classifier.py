"""Train a decision tree on simulated incidents and compare it with the rules.

The stretch goal from brief section 5: replace the hand-written signature table
with a scikit-learn classifier. The rule-based engine stays the shipped default
(see docs/evaluation.md for why); this script exists to measure whether a
learned model would be any better.

    pip install -r requirements-ml.txt
    python evaluation/train_classifier.py

Labels are free: every simulated incident records the cause that was injected.

WHAT THIS CAN AND CANNOT SHOW
    The training data comes from simulate.py, whose behaviour was written by the
    same person who wrote the signatures. A tree trained on it can only learn
    the simulator's rules back. Matching the rule engine here means "the rules
    captured the simulator's signal", not "the model is good". The comparison
    only becomes meaningful when trained and tested on incidents captured from
    the live cluster.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np                                      # noqa: E402
from sklearn.tree import DecisionTreeClassifier, export_text   # noqa: E402

import metrics as m                                     # noqa: E402
import simulate                                         # noqa: E402
from correlate import correlate, firing_only            # noqa: E402
from dependency_graph import depth, is_known            # noqa: E402
from signatures import SIGNATURES                       # noqa: E402

ALERTS = sorted({s for spec in SIGNATURES.values() for s in spec["weights"]})
PAIR_FEATURES = [f"{alert}@{service}" for alert in ALERTS for service in simulate.SERVICES]
FEATURES = PAIR_FEATURES + ["deepest_service_depth", "n_distinct_services", "n_alerts"]


def featurize(payload: dict) -> list[float]:
    """One incident -> a numeric vector.

    One binary feature per (alert, service) pair - the tree needs the service,
    not just the alert name, because the same alert means different things on
    different services - plus three aggregates.
    """
    alerts = firing_only(payload.get("alerts", []))
    pairs = {(a["labels"]["alertname"], a["labels"].get("service", "")) for a in alerts}
    services = {s for _, s in pairs if s}
    in_chain = [depth(s) for s in services if is_known(s)]

    vector = [1.0 if tuple(p.split("@")) in pairs else 0.0 for p in PAIR_FEATURES]
    vector += [
        float(min(in_chain)) if in_chain else float(len(simulate.SERVICES)),
        float(len(services)),
        float(len(alerts)),
    ]
    return vector


def matrix(dataset: list[dict]) -> tuple[np.ndarray, list[str]]:
    X = np.array([featurize(r["payload"]) for r in dataset])
    y = [m.norm(r["label"]) for r in dataset]
    return X, y


def score(name: str, truths: list[str], preds: list[str]) -> str:
    n = len(truths)
    c = m.outcome_counts(truths, preds)
    return (
        f"| {name} | {m.pct(m.accuracy(truths, preds))} | {m.pct(c['correct_action'] / n)} "
        f"| {m.pct(c['missed'] / n)} | {m.pct(c['wrong_action'] / n)} |"
    )


def main() -> None:
    train = simulate.generate(8000, simulate.PRESETS["moderate"], seed=1)
    X_train, y_train = matrix(train)

    tree = DecisionTreeClassifier(max_depth=8, min_samples_leaf=5, random_state=0)
    tree.fit(X_train, y_train)

    print("### Decision tree vs hand-written rules\n")
    print(f"Trained on {len(train)} simulated 'moderate' incidents (seed 1), "
          f"max depth {tree.get_depth()}, {tree.get_n_leaves()} leaves. "
          f"Tested on fresh incidents from different seeds.\n")
    print("**SIMULATED data throughout - see the caveat at the top of this file.**\n")

    print("| test set | model | accuracy | correct actions | missed | WRONG ACTIONS |")
    print("|---|---|---:|---:|---:|---:|")
    for label, level, seed in (("moderate, new seed", "moderate", 2), ("hard (harder than training)", "hard", 3)):
        test = simulate.generate(4000, simulate.PRESETS[level], seed=seed)
        X_test, truths = matrix(test)
        tree_preds = list(tree.predict(X_test))
        rule_preds = [m.norm((correlate(firing_only(r["payload"]["alerts"])) or {}).get("root_cause"))
                      for r in test]
        for model, preds in (("hand-written rules", rule_preds), ("decision tree", tree_preds)):
            row = score(model, truths, preds)
            print(row.replace("| ", f"| {label} | ", 1))
    print()

    print("#### What the tree relied on\n")
    importances = sorted(zip(FEATURES, tree.feature_importances_), key=lambda x: -x[1])[:8]
    print("| feature | importance |")
    print("|---|---:|")
    for feature, weight in importances:
        print(f"| {feature} | {weight:.3f} |")
    print()

    print("#### The tree as rules (top 4 levels)\n")
    print("```")
    print(export_text(tree, feature_names=FEATURES, max_depth=4))
    print("```")


if __name__ == "__main__":
    main()
