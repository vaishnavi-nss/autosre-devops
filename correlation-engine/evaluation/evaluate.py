"""Measure how well the correlation engine names root causes.

Two modes, and they answer different questions - never mix their numbers:

    python evaluation/evaluate.py --difficulty moderate --n 5000 --seed 7
        SIMULATED incidents. Tests robustness to dropped symptoms, cascades and
        noise. Says nothing about real-world accuracy (see simulate.py).

    python evaluation/evaluate.py --replay captured.jsonl
        REAL incidents captured from the live cluster. Each line is
        {"label": <cause or null>, "payload": <Alertmanager payload>}.
        Produce the file by running the engine with CAPTURE_PATH set and
        PUT /capture/label before each fault injection.

Output is markdown, so it can be pasted straight into docs/evaluation.md.
Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # engine modules
sys.path.insert(0, str(Path(__file__).resolve().parent))       # sibling modules

import metrics as m                                              # noqa: E402
import simulate                                                  # noqa: E402
from correlate import (                                          # noqa: E402
    confidence,
    correlate,
    firing_only,
    match_signature,
    services_by_depth,
)
from dependency_graph import depth                               # noqa: E402
from signatures import SIGNATURES                                # noqa: E402


def correlate_with_fallback(alerts: list[dict]) -> dict | None:
    """ABLATION ONLY - not what the engine does.

    Like the engine, but if the deepest alerting service has no matching
    signature it walks up to the next shallower service instead of escalating.
    Exists so the design choice in correlate.py ("fail safe") can be measured
    against the obvious alternative rather than merely asserted.
    """
    firing = firing_only(alerts)
    for service in services_by_depth(firing):
        cause = match_signature(service, firing)
        if cause:
            return {"root_cause": cause, "confidence": confidence(cause, firing)}
    return None


def load_replay(path: Path) -> list[dict]:
    records = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        record = json.loads(line)
        if "payload" not in record:
            raise SystemExit(f"{path}:{lineno}: record has no 'payload'")
        records.append(record)
    if not records:
        raise SystemExit(f"{path}: no records")
    return records


def run_engine(dataset: list[dict], engine=correlate):
    """Return (truths, predictions, confidences, seconds-per-incident)."""
    truths, preds, confs, timings = [], [], [], []
    for record in dataset:
        alerts = firing_only(record["payload"].get("alerts", []))
        started = time.perf_counter()
        result = engine(alerts)
        timings.append(time.perf_counter() - started)
        truths.append(m.norm(record.get("label")))
        preds.append(m.norm(result["root_cause"] if result else None))
        confs.append(result["confidence"] if result else 0.0)
    return truths, preds, confs, timings


def attribute_failures(dataset: list[dict], truths: list[str], preds: list[str]) -> str | None:
    """Explain WHY each failure happened, using the simulator's metadata.

    Only available for simulated data. Buckets that should be empty (named
    "unexplained") are a smoke alarm: if they are not, the simulator and the
    engine disagree about something and the numbers should not be trusted.
    """
    if not any("meta" in r for r in dataset):
        return None

    misses: Counter = Counter()
    wrongs: Counter = Counter()
    for record, truth, pred in zip(dataset, truths, preds):
        meta = record["meta"]
        kind = m.outcome(truth, pred)
        if kind == "missed":
            owner = SIGNATURES[truth]["owner"]
            deeper_noise = any(depth(s) < depth(owner) for _, s in meta["spurious"])
            if meta["deciding_dropped"]:
                misses["the deciding symptom never fired (alert flapped or was missed)"] += 1
            elif deeper_noise:
                misses["an unrelated alert on a deeper service blocked the match"] += 1
            else:
                misses["unexplained"] += 1
        elif kind == "wrong_action":
            if meta["confounder"] == pred:
                wrongs["a second cause's deciding symptom was also present"] += 1
            elif meta["kind"] == "deep_fault":
                wrongs["a deeper fault was mistaken for the symptom it caused"] += 1
            else:
                wrongs["unexplained"] += 1

    lines = ["#### Why the failures happened", ""]
    for title, counter in (("Missed (escalated a known cause)", misses), ("Wrong action", wrongs)):
        lines.append(f"**{title}**")
        lines.append("")
        if not counter:
            lines += ["none", ""]
            continue
        lines += ["| reason | count |", "|---|---:|"]
        lines += [f"| {reason} | {count} |" for reason, count in counter.most_common()]
        lines.append("")
    return "\n".join(lines)


def ablation(dataset: list[dict]) -> str:
    """Compare the shipped fail-safe engine with the naive fall-back variant."""
    rows = ["#### Design check: fail safe vs fall back", "",
            "The engine escalates when the deepest alerting service has no matching signature. "
            "The alternative walks up to a shallower service that does match. Same data, both engines:", "",
            "| engine | correct actions | missed | WRONG ACTIONS | wrong-action rate |",
            "|---|---:|---:|---:|---:|"]
    for name, engine in (("fail safe (shipped)", correlate), ("fall back to shallower", correlate_with_fallback)):
        truths, preds, _, _ = run_engine(dataset, engine)
        counts = m.outcome_counts(truths, preds)
        n = len(truths)
        rows.append(
            f"| {name} | {counts['correct_action']} | {counts['missed']} "
            f"| {counts['wrong_action']} | {m.pct(counts['wrong_action'] / n)} |"
        )
    rows += ["",
             "The fall-back variant recovers some missed incidents, and pays for it with wrong "
             "actions on incidents where a deeper fault is the real cause. Which wins depends on how "
             "often deeper faults actually occur - the share used here is an assumption "
             "(`simulate.KIND_MIX`), not a measurement.", ""]
    return "\n".join(rows)


def report(title: str, note: str, dataset: list[dict]) -> str:
    truths, preds, confs, timings = run_engine(dataset)
    n = len(truths)
    counts = m.outcome_counts(truths, preds)

    correct_conf = [c for t, p, c in zip(truths, preds, confs) if t == p and p != m.ESCALATE]
    wrong_conf = [c for t, p, c in zip(truths, preds, confs) if p != m.ESCALATE and t != p]

    out = [f"### {title}", "", note, "",
           f"**{n} incidents.** Accuracy: **{m.pct(m.accuracy(truths, preds))}**", ""]
    out += ["#### Outcomes", "", m.render_outcomes(counts, n), ""]
    out += ["#### Confusion matrix", "", m.render_confusion(truths, preds), ""]
    out += ["#### Per class", "", m.render_per_class(truths, preds), ""]
    out += [
        "#### What a confidence gate buys",
        "",
        "An action is taken only if a cause was named *and* confidence >= the gate; "
        "everything else escalates to a human.",
        "",
        m.render_gate_table(m.gate_table(truths, preds, confs)),
        "",
    ]

    attribution = attribute_failures(dataset, truths, preds)
    if attribution:
        out += [attribution]
        out += [ablation(dataset)]

    stats = [
        f"- mean confidence when correct: {statistics.mean(correct_conf):.2f}"
        if correct_conf else "- mean confidence when correct: n/a",
        f"- mean confidence when WRONG: {statistics.mean(wrong_conf):.2f} ({len(wrong_conf)} cases)"
        if wrong_conf else "- mean confidence when wrong: n/a (no wrong actions)",
        f"- engine time per incident: {statistics.mean(timings) * 1e6:.0f} microseconds",
    ]
    out += ["#### Diagnostics", ""] + stats + [""]
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--difficulty", choices=sorted(simulate.PRESETS), default="moderate")
    ap.add_argument("--n", type=int, default=2000, help="simulated incidents (default 2000)")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--replay", type=Path, help="evaluate captured real incidents instead")
    ap.add_argument("--all", action="store_true", help="run every difficulty preset")
    args = ap.parse_args()

    if args.replay:
        print(report(
            f"Real incidents ({args.replay.name})",
            "Captured from the live cluster. These numbers describe real behaviour.",
            load_replay(args.replay),
        ))
        return

    levels = list(simulate.PRESETS) if args.all else [args.difficulty]
    for level in levels:
        noise = simulate.PRESETS[level]
        print(report(
            f"Simulated incidents - {level}",
            f"SIMULATED (seed {args.seed}). Noise: deciding symptom seen {noise.p_deciding:.0%}, "
            f"each consequence {noise.p_support:.0%}, error cascades {noise.p_cascade:.0%}, "
            f"unrelated alerts {noise.p_spurious:.0%}, confounding cause {noise.p_confounder:.0%}. "
            f"Measures robustness to noise, not real-world accuracy.",
            simulate.generate(args.n, noise, args.seed),
        ))


if __name__ == "__main__":
    main()
