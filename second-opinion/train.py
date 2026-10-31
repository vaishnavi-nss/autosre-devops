"""Train the second-opinion scorer on RCAEval RE1 and report how well it holds up.

    python second-opinion/train.py            # needs the RE1 data: see
                                              # correlation-engine/evaluation/download_rcaeval.py

Writes models/second_opinion.joblib and models/second_opinion.json. The model sees only the
five signals our cluster exposes. Two checks run before the final fit, both leave-one-system-out
(each of Online Boutique, Sock Shop and Train Ticket is held out in turn):

  1. the usual 373-case evaluation, and
  2. the same with every case cut down to four services (the true one plus three others),
     because ours is a four-service system and RE1's have 12-100+.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import warnings
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from scoring import Scorer, service_scores        # noqa: E402

DATA = HERE.parent / "correlation-engine" / "data" / "rcaeval"
WINDOW_S = 300


def load_re1():
    table = pd.read_parquet(DATA / "cases.parquet")
    table = table[table.suite == "RE1"]
    cases = []
    for row in table.itertuples():
        path = DATA / row.case / "metrics.parquet"
        if not path.exists():
            continue
        try:
            scores = service_scores(pd.read_parquet(path), int(row.inject_time), WINDOW_S)
        except ValueError:
            continue
        if row.root_cause_service in scores:
            cases.append({"case": row.case, "system": row.system, "service": row.root_cause_service,
                          "fault": row.fault, "scores": scores})
    return cases


def shrink(case, rng, n=4):
    """Keep the true service plus n-1 others, as in a four-service system."""
    others = [s for s in case["scores"] if s != case["service"]]
    keep = [case["service"]] + rng.sample(others, min(n - 1, len(others)))
    return {**case, "scores": {s: case["scores"][s] for s in keep}}


def evaluate(cases, shrink_to=None, seed=0):
    rng = random.Random(seed)
    rows = []
    for system in sorted({c["system"] for c in cases}):
        train = [c for c in cases if c["system"] != system]
        test = [c for c in cases if c["system"] == system]
        if shrink_to:
            train = [shrink(c, rng, shrink_to) for c in train]
            test = [shrink(c, rng, shrink_to) for c in test]
        model = Scorer(seed).fit(train).calibrate(train)
        for c in test:
            service, fault, conf, _ = model.diagnose(c["scores"])
            rows.append({"system": system, "truth_fault": c["fault"], "service_ok": service == c["service"],
                         "fault_ok": fault == c["fault"], "conf": conf})
    df = pd.DataFrame(rows)
    both = df.service_ok & df.fault_ok
    return {"cases": len(df), "service_top1": round(float(df.service_ok.mean()), 3),
            "fault_accuracy": round(float(df.fault_ok.mean()), 3), "both_right": round(float(both.mean()), 3),
            "by_system": {s: round(float(both[df.system == s].mean()), 3) for s in sorted(df.system.unique())},
            "by_fault": {f: round(float(both[df.truth_fault == f].mean()), 3) for f in sorted(df.truth_fault.unique())},
            "confidence_gate_precision": {
                str(g): (round(float(both[df.conf >= g].mean()), 3) if (df.conf >= g).any() else None)
                for g in (0.3, 0.5, 0.6, 0.7)},
            "acts_at_gate": {str(g): int((df.conf >= g).sum()) for g in (0.3, 0.5, 0.6, 0.7)}}


def main():
    warnings.filterwarnings("ignore")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=HERE / "models")
    args = parser.parse_args()

    cases = load_re1()
    print(f"loaded {len(cases)} RE1 cases")
    report = {"trained_on": "RCAEval RE1 (MIT)", "cases": len(cases), "window_s": WINDOW_S,
              "leave_one_system_out": evaluate(cases),
              "leave_one_system_out_four_services": evaluate(cases, shrink_to=4)}
    print(json.dumps(report, indent=2))

    model = Scorer(0).fit(cases).calibrate(cases)
    import sklearn
    report["sklearn"] = sklearn.__version__
    args.out.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, args.out / "second_opinion.joblib", compress=3)
    (args.out / "second_opinion.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    size = (args.out / "second_opinion.joblib").stat().st_size / 1e6
    print(f"saved model ({size:.1f} MB) to {args.out}")


if __name__ == "__main__":
    main()
