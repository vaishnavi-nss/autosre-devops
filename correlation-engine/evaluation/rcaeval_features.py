"""Turn RCAEval RE1 cases into per-service symptom scores.

RE1 gives raw 1-second metrics per case, not alerts. Each metric is scored by how far
the post-injection window sits from that metric's own pre-injection behaviour, as a
robust z-score (median shift over MAD, with floors so flat metrics don't explode). The
scores are then collapsed to one number per (service, kind), where the kinds are the
symptoms our alerts are about: cpu, mem, latency, error, workload.

This replaces the "which alerts fired" input of the production engine. Thresholding the
scores gives alert-like symptoms (the replay baseline); the raw scores feed the learned
model.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

KINDS = ("cpu", "mem", "latency", "error", "workload", "diskio", "socket")
LOG_KINDS = ("lograte", "logerr")           # from container logs (RE2 and RE3 only)
ERROR_WORDS = ("error", "err=", "fail", "timeout", "timed out", "refused", "exception", "unavailable", "panic")
# Suffixes used by the three systems, folded onto the five kinds.
SUFFIX_KIND = {
    "cpu": "cpu", "mem": "mem", "error": "error",
    "latency": "latency", "latency-50": "latency", "latency-90": "latency",
    "load": "workload", "workload": "workload", "diskio": "diskio", "socket": "socket",
}
NOT_A_SERVICE = {"main"}          # the load-generator / host columns
Z_CLIP = 10_000.0       # high enough that large faults do not all tie at the ceiling
DATA_DIR = Path(__file__).resolve().parents[1] / "data" / "rcaeval"


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
    """{service: {kind: z}} for one case, using `window_s` seconds after injection."""
    metrics = metrics.sort_values("time").set_index("time").ffill().bfill()
    normal = metrics[metrics.index < inject_time]
    faulty = metrics[(metrics.index >= inject_time) & (metrics.index < inject_time + window_s)]
    if normal.empty or faulty.empty:
        raise ValueError("case has no data on one side of the injection")
    z = robust_z(normal, faulty).dropna()
    # How much the metric's variability changed: a fault can leave the median alone and still
    # make a metric jumpy (or flat). Log ratio of spreads, floored so constant metrics give 0.
    spread = np.log((faulty.std().fillna(0) + 1e-6) / (normal.std().fillna(0) + 1e-6)).clip(-10, 10)

    grouped: dict = {}
    spread_by: dict = {}
    for column, value in z.items():
        parsed = parse_column(column)
        if parsed:
            grouped.setdefault(parsed[0], {}).setdefault(parsed[1], []).append(float(value))
            spread_by.setdefault(parsed[0], {}).setdefault(parsed[1], []).append(float(spread[column]))

    out = {}
    for service, kinds in grouped.items():
        row = {}
        for kind in KINDS:
            values = kinds.get(kind)
            if not values:
                row[kind] = 0.0
            elif kind == "workload":                       # a drop matters as much as a rise
                row[kind] = max(values, key=abs)
            else:                                           # a fault is a rise
                row[kind] = max(values)
            row[kind + "_sd"] = max(spread_by[service].get(kind, [0.0]))
        out[service] = row
    return out


def log_scores(logs: pd.DataFrame, inject_time: int, window_s: int) -> dict:
    """{container: {lograte, logerr}}: how much log volume and error-looking lines rose.

    Per-second line counts before injection give a baseline; the score is the shift of the
    post-injection mean in units of that baseline's spread (floored, as for metrics).
    """
    start, end = int(logs.timestamp.min()), int(logs.timestamp.max()) + 1
    seconds = np.arange(start, end)
    is_error = logs.message.str.lower().str.contains("|".join(ERROR_WORDS), regex=True, na=False)
    out = {}
    for service, group in logs.groupby("container_name"):
        row = {}
        for kind, rows in (("lograte", group), ("logerr", group[is_error[group.index]])):
            counts = pd.Series(rows.timestamp.value_counts()).reindex(seconds, fill_value=0)
            before = counts[counts.index < inject_time]
            after = counts[(counts.index >= inject_time) & (counts.index < inject_time + window_s)]
            if before.empty or after.empty:
                row[kind] = 0.0
                continue
            scale = max(before.std(), 0.1 * before.mean(), 0.1)
            row[kind] = float(np.clip((after.mean() - before.mean()) / scale, -Z_CLIP, Z_CLIP))
        out[service] = row
    return out


def load_cases(system: str | None = None, window_s: int = 300, data_dir: Path = DATA_DIR,
               suite: str = "RE1", with_logs: bool = False):
    """Case table plus a score dict per case for one suite. Skips cases that fail to load."""
    table = pd.read_parquet(data_dir / "cases.parquet")
    table = table[table.suite == suite]
    if system:
        table = table[table.system == system]
    cases = []
    for row in table.itertuples():
        path = data_dir / row.case / "metrics.parquet"
        if not path.exists():
            continue
        try:
            scores = service_scores(pd.read_parquet(path), int(row.inject_time), window_s)
            if with_logs:
                logs_path = data_dir / row.case / "logs.parquet"
                logs = log_scores(pd.read_parquet(logs_path), int(row.inject_time), window_s)                     if logs_path.exists() else {}
                for service, row_scores in scores.items():
                    row_scores.update(logs.get(service, {k: 0.0 for k in LOG_KINDS}))
        except ValueError:
            continue
        cases.append({"case": row.case, "system": row.system, "service": row.root_cause_service,
                      "fault": row.fault, "scores": scores})
    return cases
