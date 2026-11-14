"""Score a trained model on a session it never saw.

    python second-opinion/eval_session.py docs/live-validation-run8 [--model models/own.joblib]

Leave-one-trial-out numbers (train_own.py) hold out single trials from the same sessions the model trained on.
This is the harder question: take the shipped model, and score the feature tables from a later live session.
"""

from __future__ import annotations

import argparse
import json
import sys
import warnings
from pathlib import Path

import joblib

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from train_own import load_cases, report  # noqa: E402


def predict(model, cases):
    rows = []
    for c in cases:
        service, fault, conf, _ = model.diagnose(c["scores"])
        rows.append({**c, "pred_service": service, "pred_fault": fault, "conf": conf})
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("dirs", nargs="+", help="result directories with trials.jsonl from a session the model did not train on")
    ap.add_argument("--model", type=Path, default=HERE / "models" / "own.joblib")
    ap.add_argument("--min-confidence", type=float, default=0.3)
    args = ap.parse_args(argv)
    warnings.filterwarnings("ignore")
    cases, skipped = load_cases(args.dirs)
    if not cases:
        sys.exit("no usable feature tables in " + ", ".join(args.dirs))
    model = joblib.load(args.model)
    rows = predict(model, cases)
    print(f"{len(cases)} cases from {len({c['system'] for c in cases})} trials ({skipped} skipped)")
    print(json.dumps(report(rows, args.min_confidence), indent=2))
    for r in rows:
        mark = "ok " if r["pred_service"] == r["service"] and r["pred_fault"] == r["fault"] else "MISS"
        print(f"  {mark} {r['system']:>10}  truth {r['service']}/{r['fault']}  ->  {r['pred_service']}/{r['pred_fault']} ({r['conf']:.2f})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
