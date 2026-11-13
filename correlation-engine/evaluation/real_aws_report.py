"""Turn the trials recorded by real_aws_validate.py into the live end-to-end numbers.

    python3 real_aws_report.py DIR [DIR ...]

How a trial is scored (fixed here, before any real data exists, so it cannot drift to fit the result)

  The prediction is the RULE ENGINE's diagnosis at the FIRST webhook the engine received for the failure: that is what a
  dispatcher would act on first. (The final webhook is reported alongside.)

  known-cause failure (memory_pressure -> high_memory_usage, pool_exhaustion -> db_connection_pool_exhaustion)
      named the true cause ........... correct
      named a different cause ........ wrong   (a wrong diagnosis; with no dispatcher deployed it is also the wrong
                                                 action the engine would hand over: the two are the same count here)
      escalated to a human ........... escalated (safe, but not a diagnosis)
      no webhook reached the engine .. missed
  failure with no known cause (db_outage, pod_crash, cpu_pressure, network_partition: none is among the engine's three
  causes, so escalating is the correct outcome)
      escalated ...................... correct
      named any cause ................ wrong
      no webhook reached the engine .. missed

  A trial counts only if it was not contaminated (the baseline was healthy), did not error, and the real failure was
  *effective*: a real measurement (memory ratio, pool ratio, replicas, restarts, latency, 5xx) crossed its threshold. A
  failure that never took hold is listed separately and is not scored for or against the system.

  REAL AWS END-TO-END ACCURACY = correct / scored single-fault trials, at the first decision. Healthy windows and
  two-fault trials are reported separately and are not in that number.
"""
from __future__ import annotations

import json
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

CLASSES = ["high_memory_usage", "db_connection_pool_exhaustion", "none"]


def load(dirs: list[Path]) -> list[dict]:
    out = []
    for d in dirs:
        path = d / "trials.jsonl"
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                t = json.loads(line)
                t["session"] = d.name
                out.append(t)
    return out


def scored(t: dict) -> bool:
    return (t.get("mode") == "single" and not t.get("error") and not t.get("contaminated")
            and bool(t.get("fault_effective")))


def outcome(t: dict, which: str = "first") -> str:
    """correct | wrong | escalated | missed for a single-fault trial."""
    decisions = t.get("decisions") or []
    if not decisions:
        return "missed"
    d = decisions[0] if which == "first" else decisions[-1]
    pred = d.get("rule_prediction")
    truth = t.get("truth") or []
    if not truth:                                    # no known cause: escalating is right
        return "correct" if pred is None else "wrong"
    if pred is None:
        return "escalated"
    return "correct" if pred == truth[0] else "wrong"


def predicted_class(t: dict) -> str | None:
    decisions = t.get("decisions") or []
    if not decisions:
        return None
    return decisions[0].get("rule_prediction") or "none"


def true_class(t: dict) -> str:
    return (t.get("truth") or ["none"])[0]


def pct(n: int, d: int) -> str:
    return "n/a" if d == 0 else f"{100 * n / d:.1f}% ({n}/{d})"


def prf(trials: list[dict]) -> list[list[str]]:
    rows = []
    for cls in CLASSES:
        tp = sum(1 for t in trials if true_class(t) == cls and predicted_class(t) == cls)
        fp = sum(1 for t in trials if true_class(t) != cls and predicted_class(t) == cls)
        fn = sum(1 for t in trials if true_class(t) == cls and predicted_class(t) != cls)   # includes missed
        support = tp + fn
        if support == 0 and fp == 0:
            continue
        precision = None if tp + fp == 0 else tp / (tp + fp)
        recall = None if tp + fn == 0 else tp / (tp + fn)
        f1 = None if not precision or not recall else 2 * precision * recall / (precision + recall)
        fmt = lambda v: "n/a" if v is None else f"{v:.2f}"       # noqa: E731
        rows.append([cls, str(support), fmt(precision), fmt(recall), fmt(f1)])
    return rows


def median(values: list[float]) -> str:
    values = [v for v in values if v is not None]
    return "n/a" if not values else f"{statistics.median(values):.0f} s (n={len(values)})"


def table(header: list[str], rows: list[list[str]]) -> list[str]:
    out = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    return out + ["| " + " | ".join(r) + " |" for r in rows]


def pair_result(t: dict) -> dict:
    truth = set(t.get("truth") or [])
    decisions = t.get("decisions") or []
    named = [d["rule_prediction"] for d in decisions if d.get("rule_prediction")]
    named_set = set(named)
    return {
        "both_identified": bool(truth) and truth <= named_set,
        "only_one": len(truth & named_set) == 1,
        "wrong_cause_named": sorted(named_set - truth),
        "second_unresolved": sorted(truth - named_set),
        "actions": sorted({d["playbook"] for d in decisions if d.get("playbook")}),
        "ambiguity_seen": any(len(d.get("ambiguous_causes") or []) > 1 for d in decisions),
        "rule_on_would_escalate": [bool(d.get("ambiguous_causes")) and len(d["ambiguous_causes"]) > 1
                                   and d.get("ambiguity_rule_would") == "escalate" for d in decisions],
        "live_escalated": sum(1 for d in decisions if d.get("escalated")),
        "webhooks": len(decisions),
    }


def report(dirs: list[Path]) -> str:
    trials = load(dirs)
    singles = [t for t in trials if t.get("mode") == "single"]
    valid = [t for t in singles if scored(t)]
    not_effective = [t for t in singles if not t.get("error") and not t.get("contaminated") and not t.get("fault_effective")]
    excluded = [t for t in singles if t.get("error") or t.get("contaminated")]
    pairs = [t for t in trials if t.get("mode") == "pair"]
    healthy = [t for t in trials if t.get("mode") == "healthy"]
    out = ["# Real AWS end-to-end results", "",
           f"Sessions: {', '.join(sorted({t['session'] for t in trials}))}. Single-fault trials {len(singles)}: "
           f"{len(valid)} scored, {len(not_effective)} where the real failure never took hold, {len(excluded)} excluded "
           f"(contaminated baseline or errored). Two-fault trials {len(pairs)}. Healthy windows {len(healthy)}.", ""]

    # per failure type
    by_fault: dict[str, list[dict]] = defaultdict(list)
    for t in valid:
        by_fault[t["real_fault"]].append(t)
    rows = []
    for name, ts in sorted(by_fault.items()):
        c = Counter(outcome(t) for t in ts)
        esc = sum(1 for t in ts if (t.get("decisions") or [{}])[0].get("escalated"))
        rows.append([name, str(len(ts)), str(c["correct"]), str(c["missed"]), str(c["wrong"]), str(esc),
                     pct(c["correct"], len(ts))])
    out += ["## By failure type (first decision)", ""]
    out += table(["Failure type", "Trials", "Correct", "Missed", "Wrong", "Escalated", "Accuracy"], rows) if rows else ["(no scored trials)"]
    out += ["", "`Escalated` counts every trial whose first webhook was an escalation; for a failure with no known cause "
                "that is also `Correct`. `Escalated` on a known-cause failure is neither correct nor wrong.", ""]

    total = Counter(outcome(t) for t in valid)
    final = Counter(outcome(t, "final") for t in valid)
    first_escalated = sum(1 for t in valid if (t.get("decisions") or [{}])[0].get("escalated"))
    named_decisions = [d for t in trials for d in (t.get("decisions") or []) if d.get("rule_prediction") and t.get("mode") != "healthy"]
    verdicts = Counter(d.get("second_opinion") or "none" for d in named_decisions)
    withheld = [d for d in named_decisions if d.get("gate_would_withhold")]
    recovered = sum(1 for t in trials if t.get("recovered"))
    ran = sum(1 for t in trials if t.get("mode") in ("single", "pair") and not t.get("error"))
    ambiguity_webhooks = sum(1 for t in trials for d in (t.get("decisions") or []) if len(d.get("ambiguous_causes") or []) > 1)
    out += ["## Headline metrics (single faults)", ""]
    out += table(["Metric", "Real AWS result"], [
        ["End-to-end accuracy (first decision)", pct(total["correct"], len(valid))],
        ["Correct diagnoses / correct escalations", str(total["correct"])],
        ["Misses (no webhook reached the engine)", f"{total['missed']} ({pct(total['missed'], len(valid))})"],
        ["Wrong diagnoses", f"{total['wrong']} ({pct(total['wrong'], len(valid))})"],
        ["Wrong actions (a wrong cause handed to a dispatcher)", f"{total['wrong']} ({pct(total['wrong'], len(valid))})"],
        ["Escalations (first decision)", f"{first_escalated} ({pct(first_escalated, len(valid))})"],
        ["Gate disagreements (scorer said disagree; shadow, so nothing was withheld)", f"{verdicts['disagree']} of {len(named_decisions)} named decisions"],
        ["Ambiguity escalations the rule WOULD make (replay; rule is off)", str(ambiguity_webhooks)],
        ["Successful recoveries", f"{recovered} of {ran}"],
        ["Accuracy at the final webhook (for reference)", pct(final["correct"], len(valid))],
    ])
    out += ["", f"**REAL AWS END-TO-END ACCURACY = {pct(total['correct'], len(valid))}**", "",
            "Computed as: scored single-fault trials in which the rule engine's first diagnosis was right (it named the "
            "true cause for a known-cause failure, or escalated for a failure with no known cause), divided by all scored "
            "single-fault trials. A trial is scored only if its baseline was healthy and the real failure was measured as "
            "effective. Healthy windows and two-fault trials are not in the number.", ""]

    out += ["## Precision, recall, F1 (single faults, first decision)", ""]
    out += table(["Class", "Support", "Precision", "Recall", "F1"], prf(valid)) if valid else ["(none)"]
    out += ["", "`none` is a failure with no known cause or an escalation. A missed trial is a false negative for its class.", ""]

    out += ["## Scorer and gate (shadow: nothing was withheld)", ""]
    out += table(["Second opinion on a named decision", "Count"], [[k, str(v)] for k, v in sorted(verdicts.items())])
    wrong_named = [d for t in valid for d in (t.get("decisions") or [])[:1] if d.get("rule_prediction") and outcome(t) == "wrong"]
    out += ["", f"The gate would have withheld {len(withheld)} named decisions; {sum(1 for d in withheld if d in wrong_named)} of "
                f"them were a wrong first diagnosis (a good veto), the rest were not wrong at first decision.", ""]

    out += ["## Latency (median)", ""]
    out += table(["Measure", "Median"], [
        ["Detection: fault start to first webhook at the engine", median([t.get("detection_latency_s") for t in valid])],
        ["Decision: fault start to first decision", median([t.get("first_decision_s") for t in valid])],
        ["Recovery: stop the fault to healthy again", median([t.get("recovery_s") for t in trials if t.get("recovered")])],
    ])

    out += ["", "## Two real faults together", ""]
    if pairs:
        rows = []
        for t in pairs:
            r = pair_result(t)
            rows.append([str(t["trial_id"]), t["real_fault"], "yes" if t.get("fault_effective") else "NO",
                         "yes" if r["both_identified"] else "no", "yes" if r["only_one"] else "no",
                         ", ".join(r["wrong_cause_named"]) or "-", ", ".join(r["second_unresolved"]) or "-",
                         ", ".join(r["actions"]) or "-", str(r["live_escalated"]), str(sum(r["rule_on_would_escalate"])),
                         str(r["webhooks"])])
        out += table(["Trial", "Faults", "Both effective", "Both named", "Only one named", "Wrong cause named",
                      "Unresolved fault", "Actions (playbooks)", "Live escalations", "Rule-on would escalate", "Webhooks"], rows)
    else:
        out.append("(no two-fault trials)")

    out += ["", "## Healthy windows (no fault)", ""]
    if healthy:
        minutes = sum(len(t.get("firing_samples") or []) * 0.5 for t in healthy)
        incidents = sum(len(t.get("decisions") or []) for t in healthy)
        esc = sum(1 for t in healthy for d in (t.get("decisions") or []) if d.get("escalated"))
        acts = sum(1 for t in healthy for d in (t.get("decisions") or []) if d.get("rule_prediction"))
        ever = sorted({a for t in healthy for a in (t.get("alerts_ever_firing") or [])})
        out += table(["Measure", "Result"], [["Observed", f"about {minutes:.0f} minutes"],
                                             ["False positives (incidents with no fault)", str(incidents)],
                                             ["False escalations", str(esc)], ["False actions (a cause was named)", str(acts)],
                                             ["Alerts that fired with no fault", ", ".join(ever) or "none"]])
    else:
        out.append("(no healthy window)")

    if not_effective:
        out += ["", "## Real failures that never took hold (not scored)", ""]
        out += table(["Trial", "Fault", "Peak measured", "Needed", "What was measured"],
                     [[str(t["trial_id"]), t["real_fault"], f"{v['peak']}", f"{v['threshold']}", v["measure"]]
                      for t in not_effective for v in (t.get("effect_peaks") or {}).values() if not v["effective"]])
    if excluded:
        out += ["", "## Excluded trials", ""]
        out += table(["Trial", "Fault", "Why"], [[str(t["trial_id"]), t["real_fault"],
                                                   t.get("error") or f"baseline not healthy: {t.get('still_firing')}"] for t in excluded])
    return "\n".join(out) + "\n"


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    print(report([Path(a) for a in argv[1:]]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
