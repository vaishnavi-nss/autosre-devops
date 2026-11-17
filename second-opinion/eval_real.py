"""Does retraining the second-opinion scorer on real-fault trials help, measured on trials it has not seen?

    python second-opinion/eval_real.py --old docs/live-validation-run3 ... --real docs/completion/real-fault-trials [--save own_real]

Two models are compared on the real-fault trials (recorded by `run_scenarios.py --collect`):

  current    the shipped models/own.joblib, trained on the older injected faults only. Every real trial is new to it.
  retrained  trained on the old trials plus every real trial EXCEPT the one being scored (leave one real trial out).
             The real trials' probes are held out together, so a window is never tested against its twin.

For each, the true claim (the cause that was really injected) and a wrong claim (some other cause, on its own service) are
put to `opinion.judge`, exactly as the live service does. A good scorer agrees with the true claim and does not agree with the
wrong one. With --save it also fits the retrained model on everything and writes models/<name>.joblib plus <name>.json.

Run it with the scikit-learn version the image pins (requirements.txt), or the saved model may not load there.
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

from opinion import judge          # noqa: E402
from scoring import OWN_KINDS, Scorer  # noqa: E402
from train_own import CHAIN, SIZE, TRUTH, load_cases  # noqa: E402

MIN_CONFIDENCE = 0.3


def score_rows(model, cases):
    rows = []
    for c in cases:
        service, fault, conf, _ = model.diagnose(c["scores"])
        rows.append({**c, "pred_service": service, "pred_fault": fault, "conf": conf})
    return rows


def summarise(rows, vocabulary):
    out = {"cases": len(rows), "service_ok": 0, "service_and_fault_ok": 0, "by_cause": {}, "wrong_claim": {"agree": 0, "disagree": 0, "no_opinion": 0}}
    for r in rows:
        out["service_ok"] += r["pred_service"] == r["service"]
        out["service_and_fault_ok"] += r["pred_service"] == r["service"] and r["pred_fault"] == r["fault"]
        true = judge(r["cause"], r["service"], r["pred_service"], r["pred_fault"], r["conf"], MIN_CONFIDENCE, vocabulary)["verdict"]
        bucket = out["by_cause"].setdefault(r["cause"], {"agree": 0, "disagree": 0, "no_opinion": 0, "wrong_service": 0})
        bucket[true] += 1
        bucket["wrong_service"] += r["pred_service"] != r["service"]
        wrong_cause = next(c for c in TRUTH if c is not None and c != r["cause"])
        wv = judge(wrong_cause, TRUTH[wrong_cause][0], r["pred_service"], r["pred_fault"], r["conf"], MIN_CONFIDENCE, vocabulary)["verdict"]
        out["wrong_claim"][wv] += 1
    return out


def fit(cases, seed=0):
    return Scorer(seed, OWN_KINDS, *SIZE, identity=CHAIN, service="gb").fit(cases).calibrate(cases)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--old", nargs="+", required=True, help="result directories of the older trials (what the shipped model was trained on)")
    ap.add_argument("--real", nargs="+", required=True, help="directories with trials.jsonl recorded by run_scenarios.py --collect")
    ap.add_argument("--current", type=Path, default=HERE / "models" / "own.joblib")
    ap.add_argument("--save", help="fit on everything and write models/<name>.joblib and <name>.json")
    ap.add_argument("--out", type=Path, default=HERE / "models")
    args = ap.parse_args(argv)
    warnings.filterwarnings("ignore")

    old, _ = load_cases(args.old)
    real, skipped = load_cases(args.real)
    trials = sorted({c["system"] for c in real})
    print(f"{len(old)} old cases from {len({c['system'] for c in old})} trials; {len(real)} real cases from {len(trials)} trials ({skipped} skipped)")
    if len(trials) < 4:
        sys.exit("need at least 4 real trials to hold any out")
    vocabulary = {c["fault"] for c in old + real}

    current = joblib.load(args.current)
    base = summarise(score_rows(current, real), vocabulary)

    rows = []
    for t in trials:
        model = fit(old + [c for c in real if c["system"] != t])
        rows += score_rows(model, [c for c in real if c["system"] == t])
    new = summarise(rows, vocabulary)

    result = {"real_trials": len(trials), "real_cases": len(real), "current_model": base, "retrained_model_leave_one_real_trial_out": new}
    print(json.dumps(result, indent=2))

    if args.save:
        model = fit(old + real)
        args.out.mkdir(exist_ok=True)
        joblib.dump(model, args.out / f"{args.save}.joblib")
        (args.out / f"{args.save}.json").write_text(json.dumps({
            "trained_on": [str(d) for d in args.old + args.real], "old_cases": len(old), "real_cases": len(real), "real_trials": len(trials),
            "comparison": result, "faults": sorted(vocabulary)}, indent=2), encoding="utf-8")
        print(f"saved {args.out / (args.save + '.joblib')}")
    return result


if __name__ == "__main__":
    main()
