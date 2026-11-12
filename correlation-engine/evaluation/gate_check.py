#!/usr/bin/env python3
"""Is the second opinion good enough to turn `SECOND_OPINION_MODE=gate` on?

Reads the results directories of live runs (each with `incidents.jsonl` and `trials.jsonl`, as written by
`live_validate.py`) and checks them against the bar written down in docs/evaluation.md ("The bar for gate
mode"). Pass only runs made with the current fault-start fix: earlier runs measured a bug, not the scorer.

    python evaluation/gate_check.py docs/live-validation-run8 docs/live-validation-run9 ...

Exit status 0 only if every criterion is met, so it can sit in a script. Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from live_report import assign, load_trials  # noqa: E402

# The bar. Chosen so that it cannot be met by luck on a handful of trials: with 0 vetoes in 40 decisions the
# 95% upper bound on the veto rate is about 9%, in 20 it would be about 16%.
MIN_SESSIONS = 3
MIN_DECISIONS = 40           # diagnosed records the scorer judged, correct decisions only
MIN_FIRST_DECISIONS = 20     # the first diagnosis of each trial: what a dispatcher acts on
MAX_VETOES = 0
MAX_VETO_UPPER_BOUND = 0.10
MIN_PROBES = 40
MIN_PROBE_RATE = 0.95        # true claims backed, wrong claims caught
MIN_UNKNOWN_TRIALS = 5       # trials with no known cause, where the engine must escalate


def wilson_upper(failures: int, n: int, z: float = 1.96) -> float:
    """Upper end of the 95% Wilson interval for a failure rate."""
    if n == 0:
        return 1.0
    p = failures / n
    centre = p + z * z / (2 * n)
    spread = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (centre + spread) / (1 + z * z / n)


def load_records(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def gather(dirs: list[Path]) -> dict:
    """Pool the evidence from every session."""
    out = {"sessions": 0, "decisions": [], "first": [], "probes_true": [], "probes_wrong": [],
           "ab": [], "unknown_trials": 0, "unknown_escalated": 0, "trials": 0}
    for d in dirs:
        trials = load_trials(d / "trials.jsonl")
        records = load_records(d / "incidents.jsonl")
        if not trials:
            continue
        out["sessions"] += 1
        grouped = assign(records, trials)
        for t in trials:
            if t.get("error") or t.get("contaminated"):
                continue
            out["trials"] += 1
            label = t.get("label")
            diagnosed = [r for r in grouped.get(t["trial"], []) if (r.get("response") or {}).get("root_cause")]
            if label is None:
                out["unknown_trials"] += 1
                if not diagnosed and t.get("first_incident_s") is not None:
                    out["unknown_escalated"] += 1
            for i, r in enumerate(diagnosed):
                opinion = (r["response"].get("second_opinion") or {}).get("verdict")
                if opinion is None or r["response"]["root_cause"] != label:
                    continue                      # only correct diagnoses can be falsely vetoed
                row = {"session": d.name, "trial": t["trial"], "verdict": opinion}
                out["decisions"].append(row)
                if i == 0:
                    out["first"].append(row)
            for p in t.get("probes", []):
                if "truth" in p:
                    out["probes_true"].append(p["truth"].get("verdict"))
                if "wrong" in p:
                    out["probes_wrong"].append(p["wrong"].get("verdict"))
                if "new_method" in p:             # the stale-alert A/B
                    out["ab"].append(p)
                    out["probes_true"].append(p["new_method"].get("verdict"))
                    out["probes_wrong"].append(p.get("new_method_wrong_claim", {}).get("verdict"))
    return out


def evaluate(data: dict) -> list[tuple[str, bool, str]]:
    """Each criterion as (name, met, detail)."""
    vetoes = sum(1 for r in data["decisions"] if r["verdict"] == "disagree")
    n = len(data["decisions"])
    upper = wilson_upper(vetoes, n)
    first_vetoes = sum(1 for r in data["first"] if r["verdict"] == "disagree")
    true_ok = sum(1 for v in data["probes_true"] if v == "agree")
    wrong_ok = sum(1 for v in data["probes_wrong"] if v == "disagree")
    n_true, n_wrong = len(data["probes_true"]), len(data["probes_wrong"])
    rows = [
        (f"sessions with the fix >= {MIN_SESSIONS}", data["sessions"] >= MIN_SESSIONS, f"{data['sessions']}"),
        (f"judged correct decisions >= {MIN_DECISIONS}", n >= MIN_DECISIONS, f"{n}"),
        (f"first decisions of a trial >= {MIN_FIRST_DECISIONS}", len(data["first"]) >= MIN_FIRST_DECISIONS,
         f"{len(data['first'])}"),
        (f"vetoes of correct decisions <= {MAX_VETOES}", vetoes <= MAX_VETOES and first_vetoes <= MAX_VETOES,
         f"{vetoes} of {n} ({first_vetoes} of {len(data['first'])} first decisions)"),
        (f"95% upper bound on the veto rate <= {MAX_VETO_UPPER_BOUND:.0%}", upper <= MAX_VETO_UPPER_BOUND,
         f"{upper:.1%}"),
        (f"true claims backed >= {MIN_PROBE_RATE:.0%} of >= {MIN_PROBES} probes",
         n_true >= MIN_PROBES and true_ok / max(n_true, 1) >= MIN_PROBE_RATE, f"{true_ok} of {n_true}"),
        (f"wrong claims caught >= {MIN_PROBE_RATE:.0%} of >= {MIN_PROBES} probes",
         n_wrong >= MIN_PROBES and wrong_ok / max(n_wrong, 1) >= MIN_PROBE_RATE, f"{wrong_ok} of {n_wrong}"),
        (f"unknown-cause trials >= {MIN_UNKNOWN_TRIALS}, all escalated",
         data["unknown_trials"] >= MIN_UNKNOWN_TRIALS and data["unknown_escalated"] == data["unknown_trials"],
         f"{data['unknown_escalated']} of {data['unknown_trials']} escalated"),
    ]
    return rows


def render(data: dict, rows: list[tuple[str, bool, str]]) -> str:
    out = ["# Gate-mode readiness", "", "| criterion | met | measured |", "|---|:---:|---|"]
    out += [f"| {name} | {'yes' if ok else '**no**'} | {detail} |" for name, ok, detail in rows]
    if data["ab"]:
        old_bad = sum(1 for p in data["ab"] if p.get("old_method", {}).get("verdict") == "disagree")
        new_bad = sum(1 for p in data["ab"] if p["new_method"].get("verdict") == "disagree")
        out += ["", f"Stale-alert A/B on {len(data['ab'])} incidents: the old start disagreed with the true cause "
                    f"{old_bad} times, the new start {new_bad} times."]
    ready = all(ok for _, ok, _ in rows)
    out += ["", "**READY**: every criterion is met." if ready else
            "**NOT READY**: leave `SECOND_OPINION_MODE=shadow`.", ""]
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("results", nargs="+", type=Path, help="results directories (incidents.jsonl + trials.jsonl)")
    args = ap.parse_args()
    data = gather(args.results)
    rows = evaluate(data)
    print(render(data, rows))
    return 0 if all(ok for _, ok, _ in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
