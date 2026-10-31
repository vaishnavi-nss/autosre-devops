#!/usr/bin/env python3
"""Turn live_validate.py output into a report.

    python evaluation/live_report.py --results results

Reads  results/incidents.jsonl  and  results/trials.jsonl,  writes
results/report.md  and prints it.

Two views, because an incident can be delivered more than once:

    First decision   what the dispatcher would act on. Alertmanager sends the
                     first webhook after group_wait (30s), when only the symptoms
                     that have fired so far are known. This is the headline.
    Every record     all webhooks, including later ones carrying more symptoms.

Trials that never produced an incident are reported separately rather than scored
as engine failures: the engine was never given the chance to decide, so they say
something about the alert rules and thresholds instead.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import metrics as m                                   # noqa: E402
from correlate import correlate, firing_only          # noqa: E402
from evaluate import load_replay, report              # noqa: E402


#: The dispatcher's planned minimum confidence to act (docs/evaluation.md).
GATE = 0.75


def parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def load_trials(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def assign(records: list[dict], trials: list[dict]) -> dict[int, list[dict]]:
    """Group records by trial: a record belongs to the latest trial labelled before it."""
    starts = sorted(
        ((parse(t["label_set_at"]), t["trial"]) for t in trials if "label_set_at" in t),
    )
    grouped: dict[int, list[dict]] = {t["trial"]: [] for t in trials}
    for record in records:
        when = parse(record["received_at"])
        owner = None
        for start, number in starts:
            if start <= when:
                owner = number
        if owner is not None:
            grouped[owner].append(record)
    for recs in grouped.values():
        recs.sort(key=lambda r: r["received_at"])
    return grouped


def decide(record: dict) -> tuple[str, float]:
    result = correlate(firing_only(record["payload"].get("alerts", [])))
    return (result["root_cause"], result["confidence"]) if result else (m.ESCALATE, 0.0)


def symptoms(record: dict) -> str:
    names = sorted({a["labels"]["alertname"] for a in firing_only(record["payload"]["alerts"])})
    return ", ".join(names)


def build(results: Path) -> str:
    records = load_replay(results / "incidents.jsonl")
    trials = load_trials(results / "trials.jsonl")
    grouped = assign(records, trials)

    usable = [t for t in trials if not t.get("contaminated") and "error" not in t]
    skipped = [t for t in trials if t not in usable]
    delivered = [t for t in usable if grouped[t["trial"]]]
    silent = [t for t in usable if not grouped[t["trial"]]]

    out = ["# Live validation results", "",
           "Faults were **injected** into a running cluster (`CHAOS_ENABLED` endpoints); everything "
           "after that - the alert rules, Alertmanager grouping, the webhook, the engine - is the real "
           "pipeline. These measure detection and correlation; they do not show that real failures "
           "look like the injected ones.", "",
           f"- trials run: **{len(trials)}**; usable: **{len(usable)}**"]
    if skipped:
        out.append(f"- excluded (previous trial's alerts still firing, or the trial errored): "
                   f"{', '.join('#' + str(t['trial']) for t in skipped)}")
    out += [f"- trials where an incident reached the engine: **{len(delivered)} of {len(usable)}**"]
    if silent:
        out.append("- **no incident delivered** (the fault never alerted, or the alert never arrived): "
                   + ", ".join(f"#{t['trial']} ({t['label'] or 'unknown'})" for t in silent))
    out.append("")

    rows = ["| # | injected | symptoms at first webhook | first decision | conf. | after | records "
            "| final decision | acts at 0.75 | outcome |",
            "|---:|---|---|---|---:|---:|---:|---|---:|---|"]
    first_set, all_set = [], []
    detect_times, act_times, never_acts = [], [], []
    for t in delivered:
        recs = grouped[t["trial"]]
        first, last = recs[0], recs[-1]
        cause, conf = decide(first)
        final_cause, final_conf = decide(last)
        injected = parse(t["injected_at"])
        seconds = (parse(first["received_at"]) - injected).total_seconds()
        truth = m.norm(t["label"])
        outcome = m.outcome(truth, cause).replace("_", " ")
        if cause != final_cause:
            outcome += " (revised later)"

        # When would a dispatcher with a confidence gate actually act? Not on the first
        # webhook if that carries only the deciding symptom: wait for one that clears it.
        acts = None
        for r in recs:
            r_cause, r_conf = decide(r)
            if r_cause != m.ESCALATE and r_conf >= GATE:
                acts = (r_cause, (parse(r["received_at"]) - injected).total_seconds())
                break
        if truth != m.ESCALATE:
            detect_times.append(seconds)
            (act_times if acts else never_acts).append(acts[1] if acts else t["trial"])
        act_cell = f"{acts[1]:.0f}s" if acts else "never"
        if acts and acts[0] != truth:
            act_cell += " (WRONG)"

        rows.append(
            f"| {t['trial']} | {t['label'] or 'unknown'} | {symptoms(first)} | {cause} | {conf:.2f} "
            f"| {seconds:.0f}s | {len(recs)} | {final_cause} ({final_conf:.2f}) | {act_cell} | {outcome} |"
        )
        first_set.append({"label": t["label"], "payload": first["payload"]})
        all_set.extend({"label": t["label"], "payload": r["payload"]} for r in recs)

    out += ["## Every trial", "", *rows, ""]
    if detect_times:
        out += ["## Timing, known-cause trials", "",
                f"- first incident reaches the engine: median **{statistics.median(detect_times):.0f}s** "
                f"(range {min(detect_times):.0f}-{max(detect_times):.0f}s) after injection",
                f"- first decision a dispatcher gated at {GATE:.2f} would act on: "
                + (f"median **{statistics.median(act_times):.0f}s** "
                   f"(range {min(act_times):.0f}-{max(act_times):.0f}s)" if act_times else "none")
                + f", in {len(act_times)} of {len(act_times) + len(never_acts)} trials"
                + (f"; **never reached the gate** in trials {', '.join('#' + str(n) for n in never_acts)}"
                   if never_acts else ""),
                "- the gap between the two is the cost of waiting for the consequences to fire: the deciding "
                "symptom alerts first and alone, so the first webhook usually scores about 0.55-0.60.", ""]
    if first_set:
        out += [report("First decision per trial",
                       "What the dispatcher would act on: the first webhook of each trial. REAL pipeline, "
                       "INJECTED faults.", first_set), ""]
        out += [report("Every record",
                       "All webhooks received, including later updates that carry more symptoms.", all_set), ""]
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--results", type=Path, default=Path("results"))
    args = ap.parse_args()
    text = build(args.results)
    (args.results / "report.md").write_text(text + "\n", encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
