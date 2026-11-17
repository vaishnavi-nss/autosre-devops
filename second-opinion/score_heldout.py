"""Score one or more saved scorer models on labelled real-fault trials none of them was trained on.

    python second-opinion/score_heldout.py --trials docs/completion/real-fault-trials-2 --models models/own.joblib models/own_real.joblib

Prints, per model: how often it names the right service and fault, how often the true claim is agreed with, and how often a wrong
claim (some other cause on its own service) is wrongly agreed with. Use the scikit-learn version the image pins (requirements.txt).
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

from eval_real import score_rows, summarise  # noqa: E402
from train_own import TRUTH, load_cases      # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--trials", nargs="+", required=True, help="directories with trials.jsonl (run_scenarios.py --collect)")
    ap.add_argument("--models", nargs="+", type=Path, required=True)
    args = ap.parse_args(argv)
    warnings.filterwarnings("ignore")
    cases, skipped = load_cases(args.trials)
    vocabulary = {fault for _, fault in TRUTH.values()}
    out = {"cases": len(cases), "trials": len({c["system"] for c in cases}), "skipped_trials": skipped, "models": {}}
    for path in args.models:
        rows = score_rows(joblib.load(path), cases)
        result = summarise(rows, vocabulary)
        result["misses"] = [(r["system"], r["pred_service"], r["pred_fault"], round(r["conf"], 3)) for r in rows
                            if r["pred_service"] != r["service"] or r["pred_fault"] != r["fault"]]
        out["models"][path.name] = result
    print(json.dumps(out, indent=2))
    return out


if __name__ == "__main__":
    main()
