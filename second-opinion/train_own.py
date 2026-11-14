"""Train the second-opinion scorer on this system's own labelled incidents.

    python second-opinion/train_own.py docs/live-validation-run3 [more result dirs ...]

The RCAEval model did not transfer (docs/evaluation.md): it read our latency-led faults as packet
loss and found the right service 62% of the time. This trains the same scorer on windows the
live harness recorded from our own cluster, where the answer is known because the fault was
injected. It also sees two signals RCAEval has no equivalent of, `disk` and `pool`, which are the
deciding metrics for two of our three causes.

Input is `trials.jsonl` from `live_validate.py`: each trial's `probes[*].features.scores` is the
per-service score table the model is given. Each probe is one case; a trial's probes are held out
together, so the evaluation never tests a window against its twin.

Writes models/own.joblib and models/own.json. Point the service at it with
MODEL_PATH=models/own.joblib. It stays in shadow mode until the live check below it passes.
"""

from __future__ import annotations

import argparse
import json
import sys
import warnings
from pathlib import Path

import joblib
import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from opinion import judge          # noqa: E402
from scoring import OWN_KINDS, Scorer  # noqa: E402

CHAIN = ("frontend", "api-gateway", "transaction-service", "database")

# What the injection puts wrong, per rule-engine cause: (service, fault name the model learns).
# `None` is the harness's "unknown" trial, a gateway fault no rule describes.
TRUTH = {
    "db_connection_pool_exhaustion": ("transaction-service", "db_connection_pool_exhaustion"),
    "high_memory_usage": ("transaction-service", "high_memory_usage"),
    "db_disk_full": ("database", "db_disk_full"),
    None: ("api-gateway", "gateway_fault"),
}
SIZE = (60, 40)          # trees, boosting rounds: ~100 cases need no more, and calibration refits them per trial
MIN_TRIALS = 12          # fewer than this and the numbers below mean nothing


def load_cases(dirs):
    """One case per probe that carries a feature table, grouped by trial."""
    cases, skipped = [], 0
    for d in dirs:
        path = Path(d) / "trials.jsonl"
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            trial = json.loads(line)
            label = trial.get("label")
            if trial.get("contaminated") or label not in TRUTH:
                skipped += 1
                continue
            for probe in trial.get("probes", []):
                scores = (probe.get("features") or {}).get("scores")
                if not scores or len(scores) < 2:
                    continue
                service, fault = TRUTH[label]
                cases.append({"system": f"{Path(d).name}:{trial['trial']}", "service": service, "fault": fault,
                              "cause": label, "scores": complete(scores)})
    return cases, skipped


def complete(scores):
    """Every service must carry every kind, or a missing metric would break the feature rows."""
    return {s: {k: float(v.get(k, 0.0)) for k in OWN_KINDS} for s, v in scores.items()}


def leave_one_trial_out(cases, seed=0):
    """Predictions from models that never saw the trial being scored."""
    rows = []
    for system in sorted({c["system"] for c in cases}):
        train = [c for c in cases if c["system"] != system]
        model = Scorer(seed, OWN_KINDS, *SIZE, identity=CHAIN, service="gb").fit(train).calibrate(train)
        for c in (c for c in cases if c["system"] == system):
            service, fault, conf, _ = model.diagnose(c["scores"])
            rows.append({**c, "pred_service": service, "pred_fault": fault, "conf": conf})
    return rows


def report(rows, min_confidence):
    n = len(rows)
    service_ok = sum(r["pred_service"] == r["service"] for r in rows)
    both_ok = sum(r["pred_service"] == r["service"] and r["pred_fault"] == r["fault"] for r in rows)
    out = {"cases": n, "service_accuracy": service_ok / n, "service_and_fault_accuracy": both_ok / n}
    vocabulary = {r["fault"] for r in rows}
    verdicts = {"true_claim": {"agree": 0, "disagree": 0, "no_opinion": 0},
                "wrong_claim": {"agree": 0, "disagree": 0, "no_opinion": 0}}
    for r in rows:
        if r["cause"] is not None:
            v = judge(r["cause"], r["service"], r["pred_service"], r["pred_fault"], r["conf"],
                      min_confidence, vocabulary)["verdict"]
            verdicts["true_claim"][v] += 1
        # a wrong claim: some other cause, on its own service
        wrong_cause = next(c for c in TRUTH if c is not None and c != r["cause"])
        wrong_service = TRUTH[wrong_cause][0]
        v = judge(wrong_cause, wrong_service, r["pred_service"], r["pred_fault"], r["conf"],
                  min_confidence, vocabulary)["verdict"]
        verdicts["wrong_claim"][v] += 1
    out["verdicts"] = verdicts
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("dirs", nargs="+", help="result directories containing trials.jsonl")
    ap.add_argument("--min-confidence", type=float, default=0.3)
    ap.add_argument("--out", type=Path, default=HERE / "models")
    args = ap.parse_args(argv)
    warnings.filterwarnings("ignore")

    cases, skipped = load_cases(args.dirs)
    trials = len({c["system"] for c in cases})
    print(f"{len(cases)} cases from {trials} trials ({skipped} trials skipped as contaminated or unlabelled)")
    if trials < MIN_TRIALS:
        sys.exit(f"need at least {MIN_TRIALS} trials with feature tables; run live_validate.py with this version")

    rows = leave_one_trial_out(cases)
    result = report(rows, args.min_confidence)
    print(json.dumps(result, indent=2))

    model = Scorer(0, OWN_KINDS, *SIZE, identity=CHAIN, service="gb").fit(cases).calibrate(cases)
    args.out.mkdir(exist_ok=True)
    joblib.dump(model, args.out / "own.joblib")
    (args.out / "own.json").write_text(json.dumps({
        "trained_on": [str(d) for d in args.dirs], "trials": trials, "cases": len(cases),
        "leave_one_trial_out": result, "faults": sorted({c["fault"] for c in cases})}, indent=2), encoding="utf-8")
    print(f"saved {args.out / 'own.joblib'}")
    return result


if __name__ == "__main__":
    main()
