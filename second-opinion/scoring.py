"""Turn a window of per-service metrics into symptom scores, and score them with a model.

The same code builds the training features (from RCAEval RE1) and the live features (from
Prometheus), so the two cannot drift apart. Each metric is scored by how far the incident
window sits from that metric's own earlier behaviour - a robust z-score (median shift over
spread, with floors so flat metrics do not explode) - and the scores are collapsed to one
number per (service, kind).

Only the five signals our cluster actually exposes are used: cpu, mem, latency, error,
workload. Features that depend on how many services a system has are left out on purpose:
RCAEval systems have 12-100+ services, ours has four.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

KINDS = ("cpu", "mem", "latency", "error", "workload")
# Column suffix -> kind. RCAEval uses latency-50/-90 and load/workload; Prometheus data is
# named with the plain kinds.
SUFFIX_KIND = {
    "cpu": "cpu", "mem": "mem", "error": "error", "workload": "workload", "load": "workload",
    "latency": "latency", "latency-50": "latency", "latency-90": "latency",
}
NOT_A_SERVICE = {"main"}              # RCAEval's load-generator / host columns
Z_CLIP = 10_000.0


def parse_column(name: str):
    """'cartservice_latency-90' -> ('cartservice', 'latency'); None if not a symptom."""
    service, _, suffix = name.rpartition("_")
    kind = SUFFIX_KIND.get(suffix)
    if not service or kind is None or service in NOT_A_SERVICE:
        return None
    return service, kind


def robust_z(normal: pd.DataFrame, faulty: pd.DataFrame) -> pd.Series:
    """Signed shift of each column's median, in units of its normal-period spread."""
    base = normal.median()
    mad = (normal - base).abs().median() * 1.4826
    scale = np.maximum.reduce([
        mad.to_numpy(), normal.std().fillna(0).to_numpy(),
        0.05 * base.abs().to_numpy(), np.full(len(base), 1e-6),
    ])
    z = (faulty.median() - base).to_numpy() / scale
    return pd.Series(np.clip(z, -Z_CLIP, Z_CLIP), index=base.index)


def service_scores(metrics: pd.DataFrame, inject_time: int, window_s: int) -> dict:
    """{service: {kind: z}} using `window_s` seconds from `inject_time` against what came before."""
    metrics = metrics.sort_values("time").set_index("time").ffill().bfill()
    normal = metrics[metrics.index < inject_time]
    faulty = metrics[(metrics.index >= inject_time) & (metrics.index < inject_time + window_s)]
    if normal.empty or faulty.empty:
        raise ValueError("no data on one side of the incident start")
    z = robust_z(normal, faulty).dropna()

    grouped: dict = {}
    for column, value in z.items():
        parsed = parse_column(column)
        if parsed:
            grouped.setdefault(parsed[0], {}).setdefault(parsed[1], []).append(float(value))

    out = {}
    for service, kinds in grouped.items():
        row = {}
        for kind in KINDS:
            values = kinds.get(kind)
            if not values:
                row[kind] = 0.0
            elif kind == "workload":                      # a drop matters as much as a rise
                row[kind] = max(values, key=abs)
            else:                                         # a fault is a rise
                row[kind] = max(values)
        out[service] = row
    return out


# ------------------------------------------------------------------ features
def service_rows(scores: dict):
    """One feature row per candidate service: its scores, their ratio to the case's top, and
    whether it is the top. Nothing here depends on how many services there are."""
    top = {k: max(v[k] for v in scores.values()) for k in KINDS}
    names, rows = [], []
    for service, v in scores.items():
        row = [v[k] for k in KINDS]
        row += [v[k] / top[k] if top[k] > 1e-6 else 0.0 for k in KINDS]
        row += [float(v[k] >= top[k] - 1e-9) for k in KINDS]
        names.append(service)
        rows.append(row)
    return names, np.array(rows)


def fault_row(scores: dict, service: str):
    mine = scores[service]
    return [mine[k] for k in KINDS] + [max(v[k] for v in scores.values()) for k in KINDS]


def _logit(p):
    p = np.clip(p, 1e-4, 1 - 1e-4)
    return np.log(p / (1 - p))


class Scorer:
    """Ranks services as the root cause, then names the fault, with a calibrated confidence."""

    def __init__(self, seed: int = 0):
        from sklearn.calibration import CalibratedClassifierCV
        from sklearn.ensemble import GradientBoostingClassifier, RandomForestClassifier
        self.seed = seed
        self.service_model = RandomForestClassifier(n_estimators=200, min_samples_leaf=2,
                                                    class_weight="balanced", random_state=seed)
        self.fault_model = CalibratedClassifierCV(
            GradientBoostingClassifier(n_estimators=120, max_depth=2, learning_rate=0.1, random_state=seed),
            method="sigmoid", cv=3)
        self.scaler = None

    def fit(self, cases):
        X, y = [], []
        for case in cases:
            names, rows = service_rows(case["scores"])
            for name, row in zip(names, rows):
                X.append(row)
                y.append(int(name == case["service"]))
        self.service_model.fit(np.array(X), np.array(y))
        self.fault_model.fit(np.array([fault_row(c["scores"], c["service"]) for c in cases]),
                             [c["fault"] for c in cases])
        return self

    def calibrate(self, cases):
        """Platt-scale the raw confidence on predictions made by models that did not train on
        the system being scored (each system held out in turn)."""
        from sklearn.linear_model import LogisticRegression
        raw, ok = [], []
        for system in sorted({c["system"] for c in cases}):
            inner = Scorer(self.seed).fit([c for c in cases if c["system"] != system])
            for c in (c for c in cases if c["system"] == system):
                service, fault, conf, _ = inner.diagnose(c["scores"], calibrated=False)
                raw.append(conf)
                ok.append(service == c["service"] and fault == c["fault"])
        # With every held-out prediction right (or every one wrong) there is nothing to fit;
        # keep the raw confidence rather than invent a mapping.
        self.scaler = (LogisticRegression().fit(_logit(np.array(raw)).reshape(-1, 1), ok)
                       if len(set(ok)) == 2 else None)
        return self

    def diagnose(self, scores: dict, calibrated: bool = True):
        """(service, fault, confidence, ranking). Raw confidence is P(service) x P(fault)."""
        names, rows = service_rows(scores)
        p = self.service_model.predict_proba(rows)[:, 1]
        best = int(np.argmax(p))
        p_service = p[best] / max(p.sum(), 1e-9)
        proba = self.fault_model.predict_proba([fault_row(scores, names[best])])[0]
        j = int(np.argmax(proba))
        conf = float(p_service * proba[j])
        if calibrated and self.scaler is not None:
            conf = float(self.scaler.predict_proba(_logit(np.array([conf])).reshape(-1, 1))[0, 1])
        ranking = sorted(zip(names, p.tolist()), key=lambda x: -x[1])
        return names[best], str(self.fault_model.classes_[j]), conf, ranking
