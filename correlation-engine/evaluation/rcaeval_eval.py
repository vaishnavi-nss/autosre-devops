"""Evaluate rule-style diagnosis and a learned model on RCAEval RE1.

    python evaluation/rcaeval_eval.py [--window 300] [--out report.md]

Two diagnosers, scored on the same held-out systems, on data the authors of this project
did not write:

  replay baseline  hand-written rules in the style of correlate.py: a metric "fires" when its
                   score passes a threshold; resource symptoms (cpu, mem) outrank latency;
                   latency with errors is packet loss, latency alone is delay; nothing
                   firing escalates. The rules are fixed in advance. Only the threshold is
                   chosen, on Online Boutique alone, and then frozen for the other systems.
  learned model    gradient boosting ranks services as the root cause, then a calibrated
                   classifier names the fault. Features are system-agnostic (symptom
                   scores, ranks within the case), so a model trained on two systems can
                   be tried on the third.

Evaluation is leave-one-system-out: each system is held out in turn. The headline measure
is the wrong-action rate - acting on the wrong service or the wrong fault - not accuracy.

The disk faults in RE1 are disk I/O stress and there is no disk metric, so the rules have
no signature for them: acting on one is a wrong action, escalating is correct. That is a
property of the data, not of the rules, and the report says so.
"""

from __future__ import annotations

import argparse
import warnings
from pathlib import Path

import numpy as np

from rcaeval_features import KINDS, LOG_KINDS, load_cases

EXTRA = tuple(k + "_sd" for k in KINDS)
USE_EXTRA = False
FAULTS = ["cpu", "mem", "disk", "delay", "loss"]       # replaced by the suite's own list in run()
RULE_FAULTS = {"cpu", "mem", "delay", "loss"}       # faults the rules have a signature for
USE_LOGS = False


def active_kinds():
    return KINDS + (LOG_KINDS if USE_LOGS else ())
THRESHOLDS = (3.0, 5.0, 8.0, 12.0, 20.0, 50.0, 100.0)
GATES = (0.0, 0.5, 0.6, 0.7, 0.8, 0.9)


# ------------------------------------------------------------------ replay baseline
def rule_diagnose(scores: dict, threshold: float):
    """(service, fault) or None to escalate."""
    def fires(service, kind):
        return scores[service][kind] > threshold

    resource_kinds = ("cpu", "mem") + (("diskio", "socket") if "disk" in RULE_FAULTS else ())
    resource = [s for s in scores if any(fires(s, k) for k in resource_kinds)]
    if resource:
        # strongest resource symptom on the strongest service; the fault is that symptom
        service = max(resource, key=lambda s: max(scores[s][k] for k in resource_kinds))
        kind = max(resource_kinds, key=lambda k: scores[service][k])
        return service, {"diskio": "disk"}.get(kind, kind)
    slow = [s for s in scores if fires(s, "latency")]
    if slow:
        service = max(slow, key=lambda s: scores[s]["latency"])
        return service, "loss" if fires(service, "error") else "delay"
    return None


def outcome(truth_service, truth_fault, decision, in_vocabulary=True):
    if decision is None:
        return "correct escalation" if not in_vocabulary else "missed"
    if decision == (truth_service, truth_fault):
        return "correct action"
    return "wrong action"


# ------------------------------------------------------------------ learned model
def service_rows(case):
    """One feature row per candidate service in a case."""
    scores = case["scores"]
    kinds = active_kinds()
    top = {k: max(v[k] for v in scores.values()) for k in kinds}
    spread = sum(v["latency"] > 5 for v in scores.values())
    names, rows = [], []
    for service, v in scores.items():
        row = [v[k] for k in kinds]
        row += [v[k] / top[k] if top[k] > 1e-6 else 0.0 for k in kinds]
        row += [float(v[k] >= top[k] - 1e-9) for k in kinds]
        row += [float(spread), float(len(scores))]
        if USE_EXTRA:
            row += [v[k] for k in EXTRA]
        names.append(service)
        rows.append(row)
    return names, np.array(rows)


def fault_row(case, service):
    scores = case["scores"]
    mine = scores[service]
    kinds = active_kinds()
    row = [mine[k] for k in kinds]
    row += [max(v[k] for v in scores.values()) for k in kinds]
    row += [sum(v["latency"] > 5 for v in scores.values()), sum(v["error"] > 5 for v in scores.values()),
            sum(v["cpu"] > 5 for v in scores.values())]
    if USE_EXTRA:
        row += [mine[k] for k in EXTRA]
    return row


class Learned:
    def __init__(self, seed=0):
        from sklearn.calibration import CalibratedClassifierCV
        from sklearn.ensemble import GradientBoostingClassifier, RandomForestClassifier
        self.seed = seed
        self.service_model = RandomForestClassifier(n_estimators=300, min_samples_leaf=2,
                                                    class_weight="balanced", random_state=seed)
        self.fault_model = CalibratedClassifierCV(
            GradientBoostingClassifier(n_estimators=120, max_depth=2, learning_rate=0.1, random_state=seed),
            method="sigmoid", cv=3)

    def fit(self, cases):
        X, y = [], []
        for case in cases:
            names, rows = service_rows(case)
            for name, row in zip(names, rows):
                X.append(row)
                y.append(int(name == case["service"]))
        self.service_model.fit(np.array(X), np.array(y))
        self.fault_model.fit(np.array([fault_row(c, c["service"]) for c in cases]),
                             [c["fault"] for c in cases])
        return self

    def calibrate(self, cases):
        """Fit a map from raw confidence to P(correct) using predictions the model did not train on.

        Each training system is held out in turn; a model trained on the rest scores it. Those
        out-of-fold (confidence, correct) pairs are what the Platt scaler is fitted to, so the test
        system never influences it.
        """
        from sklearn.linear_model import LogisticRegression
        raw, ok = [], []
        for system in sorted({c["system"] for c in cases}):
            inner = Learned(self.seed).fit([c for c in cases if c["system"] != system])
            for c in (c for c in cases if c["system"] == system):
                service, fault, conf = inner.diagnose(c, calibrated=False)
                raw.append(conf)
                ok.append(service == c["service"] and fault == c["fault"])
        self.scaler = LogisticRegression().fit(_logit(np.array(raw)).reshape(-1, 1), ok)
        return self

    def diagnose(self, case, calibrated=True):
        """(service, fault, confidence): raw confidence is P(service) x P(fault), then Platt-scaled."""
        names, rows = service_rows(case)
        p = self.service_model.predict_proba(rows)[:, 1]
        best = int(np.argmax(p))
        p_service = p[best] / max(p.sum(), 1e-9)
        proba = self.fault_model.predict_proba([fault_row(case, names[best])])[0]
        j = int(np.argmax(proba))
        conf = float(p_service * proba[j])
        if calibrated and getattr(self, "scaler", None) is not None:
            conf = float(self.scaler.predict_proba(_logit(np.array([conf])).reshape(-1, 1))[0, 1])
        return names[best], self.fault_model.classes_[j], conf


def _logit(p):
    p = np.clip(p, 1e-4, 1 - 1e-4)
    return np.log(p / (1 - p))


def ece(confidences, correct, bins=5):
    """Expected calibration error: how far stated confidence is from observed accuracy."""
    confidences, correct = np.asarray(confidences), np.asarray(correct, dtype=float)
    edges = np.linspace(0, 1, bins + 1)
    total = 0.0
    for lo, hi in zip(edges[:-1], edges[1:]):
        mask = (confidences > lo) & (confidences <= hi) if lo else (confidences <= hi)
        if mask.any():
            total += mask.mean() * abs(correct[mask].mean() - confidences[mask].mean())
    return float(total)


# ------------------------------------------------------------------ evaluation
def tally(outcomes):
    n = len(outcomes)
    return {k: sum(o == k for o in outcomes) for k in
            ("correct action", "correct escalation", "missed", "wrong action")} | {"n": n}


def pick_threshold(cases):
    """Threshold with the fewest wrong actions on `cases`, then the most correct ones."""
    def key(t):
        outs = [outcome(c["service"], c["fault"], rule_diagnose(c["scores"], t), c["fault"] in RULE_FAULTS)
                for c in cases]
        return (tally(outs)["wrong action"], -tally(outs)["correct action"])
    return min(THRESHOLDS, key=key)


def run(window_s: int, suite: str = "RE1"):
    global FAULTS, RULE_FAULTS
    cases = load_cases(window_s=window_s, suite=suite, with_logs=USE_LOGS)
    FAULTS = sorted({c["fault"] for c in cases}, key=lambda f: ["cpu", "mem", "disk", "delay", "loss", "socket"].index(f))
    RULE_FAULTS = {"cpu", "mem", "delay", "loss"} | ({"disk", "socket"} if suite == "RE2" else set())
    systems = sorted({c["system"] for c in cases})
    names = {"ob": "Online Boutique", "ss": "Sock Shop", "tt": "Train Ticket"}
    lines = [f"# RCAEval {suite} evaluation (first {window_s}s after injection{', with logs' if USE_LOGS else ''})", "",
             f"{len(cases)} cases, {len(systems)} systems. The faults are real injections; the "
             "diagnosers see only metric scores, never the labels.", ""]

    # --- replay baseline: threshold fixed on Online Boutique, applied everywhere
    threshold = pick_threshold([c for c in cases if c["system"] == "ob"])
    lines += [f"## Replay baseline (threshold {threshold:g}, chosen on Online Boutique only)", "",
              "| system | cases | correct action | correct escalation | missed | wrong action |",
              "|---|---:|---:|---:|---:|---:|"]
    baseline_by_fault: dict = {}
    for system in systems:
        part = [c for c in cases if c["system"] == system]
        outs = []
        for c in part:
            o = outcome(c["service"], c["fault"], rule_diagnose(c["scores"], threshold),
                        c["fault"] in RULE_FAULTS)
            outs.append(o)
            baseline_by_fault.setdefault(c["fault"], []).append(o)
        t = tally(outs)
        lines.append(f"| {names[system]}{' (tuned here)' if system == 'ob' else ''} | {t['n']} | "
                     f"{t['correct action']} | {t['correct escalation']} | {t['missed']} | {t['wrong action']} |")
    lines += ["", "By fault (all systems):", "", "| fault | cases | correct action | correct escalation | missed | wrong action |",
              "|---|---:|---:|---:|---:|---:|"]
    for fault in FAULTS:
        t = tally(baseline_by_fault.get(fault, []))
        lines.append(f"| {fault} | {t['n']} | {t['correct action']} | {t['correct escalation']} | {t['missed']} | {t['wrong action']} |")

    # --- learned model: leave one system out
    lines += ["", "## Learned model (leave one system out)", "",
              "Trained on the other two systems, tested on the held-out one.", "",
              "| held-out system | cases | service top-1 | fault accuracy | both right | wrong-action rate at gate 0.0 |",
              "|---|---:|---:|---:|---:|---:|"]
    pooled = []                                   # (confidence, service_ok, fault_ok, fault) across folds
    by_fault = {f: [0, 0] for f in FAULTS}
    confusion: dict = {}
    for system in systems:
        train = [c for c in cases if c["system"] != system]
        test = [c for c in cases if c["system"] == system]
        model = Learned().fit(train).calibrate(train)
        rows = []
        for c in test:
            service, fault, conf = model.diagnose(c)
            rows.append((conf, service == c["service"], fault == c["fault"], c["fault"]))
            confusion[(c["fault"], fault)] = confusion.get((c["fault"], fault), 0) + 1
        pooled += rows
        for _, s_ok, f_ok, fault in rows:
            by_fault[fault][0] += int(s_ok and f_ok)
            by_fault[fault][1] += 1
        s_acc = np.mean([r[1] for r in rows]); f_acc = np.mean([r[2] for r in rows])
        both = np.mean([r[1] and r[2] for r in rows])
        lines.append(f"| {names[system]} | {len(rows)} | {s_acc:.1%} | {f_acc:.1%} | {both:.1%} | {1 - both:.1%} |")
    both_all = [r[1] and r[2] for r in pooled]
    lines += ["", f"Pooled over all held-out folds: {len(pooled)} cases, both right **{np.mean(both_all):.1%}**.",
              "", "By fault, both right:", "", "| fault | cases | both right |", "|---|---:|---:|"]
    for fault in FAULTS:
        ok, n = by_fault[fault]
        lines.append(f"| {fault} | {n} | {ok / n:.1%} |" if n else f"| {fault} | 0 | n/a |")

    lines += ["", "Fault confusion (rows = true, columns = predicted; service ignored):", "",
              "| true / predicted | " + " | ".join(FAULTS) + " |", "|---|" + "---:|" * len(FAULTS)]
    for t in FAULTS:
        lines.append(f"| **{t}** | " + " | ".join(str(confusion.get((t, p), 0)) for p in FAULTS) + " |")

    conf = np.array([r[0] for r in pooled])
    lines += ["", "### Does confidence mean anything?", "",
              f"Expected calibration error: **{ece(conf, both_all):.3f}** (0 = confidence equals accuracy). "
              f"Mean confidence {conf.mean():.2f}, observed accuracy {np.mean(both_all):.2f}.", "",
              "Acting only above a confidence gate (pooled held-out cases):", "",
              "| gate | acts on | wrong actions | of all cases wrong | precision of actions |",
              "|---:|---:|---:|---:|---:|"]
    for gate in GATES:
        acts = conf >= gate
        n_act = int(acts.sum())
        wrong = int((acts & ~np.array(both_all)).sum())
        lines.append(f"| {gate:.1f} | {n_act} ({n_act / len(conf):.0%}) | {wrong} | {wrong / len(conf):.1%} | "
                     f"{(n_act - wrong) / n_act:.1%} |" if n_act else f"| {gate:.1f} | 0 | 0 | 0.0% | n/a |")
    return "\n".join(lines) + "\n"


def main():
    warnings.filterwarnings("ignore")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--window", type=int, default=300, help="seconds of post-injection data used")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--suite", default="RE1", choices=("RE1", "RE2"))
    parser.add_argument("--logs", action="store_true", help="add log-volume and log-error features (RE2)")
    parser.add_argument("--spread", action="store_true", help="add the variability features (did not help)")
    args = parser.parse_args()
    global USE_EXTRA, USE_LOGS
    USE_EXTRA = args.spread
    USE_LOGS = args.logs
    text = run(args.window, args.suite)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
