"""Retrain the scorer on the older trials plus the Kubernetes-state fault trials, and keep only the fault kinds that are reliably detectable.

    python second-opinion/retrain_with_state_faults.py \
        --old docs/live-validation-run3 docs/live-validation-run4 docs/live-validation-run5-early docs/live-validation-run8 \
        --new docs/completion/state-fault-trials/A docs/completion/state-fault-trials/A2 docs/completion/state-fault-trials/B

1. Leave-one-trial-out: every trial is predicted by a model that never saw it. This gives, per kind of fault, the share of held-out windows where the scorer named the
   right service AND the right kind of fault.
2. Keep the kinds at or above --bar (default 0.95, at least 16 held-out windows). The others are left out of training, so `opinion.judge` answers `no_opinion` for the
   causes that map to them (the dispatcher does not treat that as a veto for the state causes).
3. Check what `judge` would have said on the held-out windows of the kept kinds: how many true claims it backs, how many wrong claims it catches.
4. Fit and calibrate the final model on everything that is kept and write models/own_v2.joblib and models/own_v2.json.

The model learns the KIND of fault (service down, bad rollout, unreachable, paused, Service type changed), on whichever service it happened; `opinion.KIND_OF` maps each
rule-engine cause to its kind. Needs scikit-learn, numpy, pandas and joblib (the versions in second-opinion/requirements.txt).
"""

from __future__ import annotations

import argparse
import json
import sys
import warnings
from collections import Counter, defaultdict
from pathlib import Path

import joblib

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import train_own as t            # noqa: E402
from opinion import KIND_OF, judge  # noqa: E402
from scoring import ALL_KINDS, Scorer  # noqa: E402


def held_out_rows(cases):
    rows = []
    for system in sorted({c["system"] for c in cases}):
        train = [c for c in cases if c["system"] != system]
        model = Scorer(0, ALL_KINDS, *t.SIZE, identity=t.CHAIN, service="gb").fit(train)      # no calibration: only the prediction is judged here
        for c in (c for c in cases if c["system"] == system):
            service, fault, conf, _ = model.diagnose(c["scores"], calibrated=False)
            rows.append({"cause": str(c["cause"]), "fault": c["fault"], "system": system, "service_ok": service == c["service"],
                         "both_ok": service == c["service"] and fault == c["fault"], "pred_service": service, "pred_fault": str(fault), "conf": float(conf)})
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--old", nargs="*", default=[], help="result directories of the earlier trials (trials.jsonl)")
    ap.add_argument("--new", nargs="+", required=True, help="result directories of the state-fault trials")
    ap.add_argument("--bar", type=float, default=0.95)
    ap.add_argument("--out", type=Path, default=HERE / "models")
    args = ap.parse_args(argv)
    warnings.filterwarnings("ignore")

    cases, _ = t.load_cases(args.old + args.new)
    rows = held_out_rows(cases)
    by_kind = defaultdict(list)
    for r in rows:
        by_kind[r["fault"]].append(r["both_ok"])
    keep = {k for k, v in by_kind.items() if len(v) >= 16 and sum(v) / len(v) >= args.bar}
    dropped = sorted(set(by_kind) - keep)
    print("held out: service", round(sum(r["service_ok"] for r in rows) / len(rows), 3), "service+fault", round(sum(r["both_ok"] for r in rows) / len(rows), 3))
    for k, v in sorted(by_kind.items()):
        print(f"  {k:30s} {len(v):4d} windows  {sum(v) / len(v):.2f}  {'kept' if k in keep else 'DROPPED'}")

    judged = [c for c in {r["cause"] for r in rows} if c != "None" and (c in keep or KIND_OF.get(c) in keep)]
    true_v, wrong_v = Counter(), Counter()
    for r in rows:
        c = r["cause"]
        if c not in judged:
            continue
        other = next(x for x in sorted(judged) if KIND_OF.get(x, x) != KIND_OF.get(c, c))
        true_v[judge(c, t.TRUTH[c][0], r["pred_service"], r["pred_fault"], r["conf"], 0.3, keep)["verdict"]] += 1
        wrong_v[judge(other, t.TRUTH[c][0], r["pred_service"], r["pred_fault"], r["conf"], 0.3, keep)["verdict"]] += 1
    print("judge on held-out windows of the kept kinds: true claims", dict(true_v), "| wrong claims", dict(wrong_v))

    final = [c for c in cases if c["fault"] in keep or c["fault"] == "gateway_fault"]      # gateway_fault stays as the 'something else' class
    model = Scorer(0, ALL_KINDS, *t.SIZE, identity=t.CHAIN, service="gb").fit(final).calibrate(final)
    args.out.mkdir(exist_ok=True)
    joblib.dump(model, args.out / "own_v2.joblib")
    (args.out / "own_v2.json").write_text(json.dumps({
        "trained_on": args.old + args.new, "trials": len({c["system"] for c in final}), "cases": len(final), "faults": sorted({c["fault"] for c in final}),
        "kinds_kept_bar": args.bar, "kinds_dropped": dropped,
        "leave_one_trial_out": {"windows": len(rows), "service": round(sum(r["service_ok"] for r in rows) / len(rows), 3),
                                "service_and_fault": round(sum(r["both_ok"] for r in rows) / len(rows), 3),
                                "by_kind": {k: {"windows": len(v), "accuracy": round(sum(v) / len(v), 3)} for k, v in sorted(by_kind.items())}},
        "judge_on_held_out_windows": {"true_claims": dict(true_v), "wrong_claims": dict(wrong_v)}}, indent=2), encoding="utf-8")
    print("saved", args.out / "own_v2.joblib")


if __name__ == "__main__":
    main()
