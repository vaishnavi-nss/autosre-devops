"""Read two decisions off recorded incidents: the ambiguity rule, and the gate.

    python evaluation/ambiguity_report.py cost  d11 d12 d13 d14 d15    # single-fault sessions
    python evaluation/ambiguity_report.py pairs d16 [d17 ...]          # two-fault sessions

Both replay every recorded incident through the engine's own `correlate()`, once with `escalate_on_ambiguity` off and
once on. That is exact, not an estimate: the rule is a pure function of the firing alerts.

`cost`: on single-fault incidents with a known label, what turning the rule on would change. A correct action turned
into an escalation is the cost; a wrong action turned into an escalation is the benefit.

`pairs`: on trials that inject two known faults together (both real), what the engine did live (acted on which cause,
or escalated, or the gate withheld it), what the rule would have done, and what the scorer said. With two real faults
any single diagnosis is incomplete, so "acts on one cause of the pair" is partial, and "acts on a cause that is not
in the pair" is a wrong action.
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from correlate import correlate, firing_only  # noqa: E402
from gate_check import load_records  # noqa: E402
from live_report import assign, load_trials  # noqa: E402


def decide(record: dict, rule: bool) -> str | None:
    """The cause the engine would name for this incident's alerts, with the ambiguity rule on or off."""
    result = correlate(firing_only(record["payload"].get("alerts", [])), rule)
    return result["root_cause"] if result else None


def outcome(cause: str | None, label: str | None) -> str:
    if cause is None:
        return "escalated" if label is None else "missed"
    return "correct" if cause == label else "wrong"


def cost(dirs: list[Path]) -> str:
    rows, first_rows = [], []
    for d in dirs:
        trials = [t for t in load_trials(d / "trials.jsonl")
                  if not t.get("error") and not t.get("contaminated") and t.get("mode") != "pair"]
        grouped = assign(load_records(d / "incidents.jsonl"), trials)
        for t in trials:
            for i, r in enumerate(grouped.get(t["trial"], [])):
                if not firing_only(r["payload"].get("alerts", [])):
                    continue
                row = (outcome(decide(r, False), t.get("label")), outcome(decide(r, True), t.get("label")))
                rows.append(row)
                if i == 0:
                    first_rows.append(row)

    def block(title: str, data: list[tuple[str, str]]) -> list[str]:
        off, on = Counter(a for a, _ in data), Counter(b for _, b in data)
        changed = Counter((a, b) for a, b in data if a != b)
        out = [f"**{title}** ({len(data)} incidents)", "", "| outcome | rule off | rule on |", "|---|---:|---:|"]
        out += [f"| {k} | {off[k]} | {on[k]} |" for k in ("correct", "wrong", "missed", "escalated")]
        out.append("")
        out.append("Changed by the rule: " + (", ".join(f"{n} x {a} -> {b}" for (a, b), n in sorted(changed.items()))
                                              or "nothing"))
        return out + [""]

    return "\n".join(["# Ambiguity rule on single-fault incidents", "",
                      f"Sessions: {', '.join(d.name for d in dirs)}. Contaminated and errored trials excluded.", ""]
                     + block("Every recorded webhook", rows) + block("First webhook of each trial", first_rows))


def _t(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp)


def pairs(dirs: list[Path]) -> str:
    out = ["# Two simultaneous faults", ""]
    totals: Counter = Counter()
    probes: Counter = Counter()
    for d in dirs:
        cond = d / "conditions.json"
        out.append(f"## {d.name}" + (f"  (`{cond.read_text(encoding='utf-8').strip().replace(chr(10), ' ')}`)"
                                      if cond.exists() else ""))
        trials = load_trials(d / "trials.jsonl")
        records = load_records(d / "incidents.jsonl")
        out += ["", "| trial | pair | webhook | live engine | scorer | rule off | rule on |", "|---|---|---|---|---|---|---|"]
        for t in trials:
            if t.get("mode") != "pair" or t.get("error"):
                continue
            tag = " (contaminated)" if t.get("contaminated") else ""
            start, end = _t(t["label_set_at"]), _t(t["reset_at"]) if t.get("reset_at") else None
            mine = [r for r in records if _t(r["received_at"]) >= start and (end is None or _t(r["received_at"]) <= end)]
            for i, r in enumerate(mine):
                resp = r.get("response") or {}
                live = resp.get("root_cause") or (f"withheld ({resp.get('rule_root_cause')})"
                                                  if resp.get("status") == "vetoed" else "escalated")
                verdict = (resp.get("second_opinion") or {}).get("verdict", "-")
                off, on = decide(r, False), decide(r, True)
                out.append(f"| {t['trial']}{tag} | {t['label']} | {i + 1} | {live} | {verdict} | {off or 'escalate'} "
                           f"| {on or 'escalate'} |")
                if t.get("contaminated"):
                    continue
                totals["webhooks"] += 1
                if off is None:
                    totals["off escalates"] += 1
                else:
                    totals["off acts on a cause in the pair" if off in t["pair"] else "off acts on a cause NOT in the pair"] += 1
                totals["on escalates" if on is None else "on acts"] += 1
                if resp.get("status") == "vetoed":
                    totals["live: gate withheld"] += 1
                elif resp.get("root_cause"):
                    totals["live: acted, cause in pair" if resp["root_cause"] in t["pair"]
                           else "live: acted, cause NOT in pair"] += 1
                else:
                    totals["live: escalated by the rules"] += 1
            if not t.get("contaminated"):
                for p in t.get("probes", []):
                    for cause, v in p["truth"].items():
                        probes[f"true cause {cause}: {v.get('verdict')}"] += 1
                    probes[f"absent cause: {p['wrong'].get('verdict')}"] += 1
        out.append("")
    out += ["## Totals (counted trials, every webhook)", "", "| measure | count |", "|---|---:|"]
    out += [f"| {k} | {v} |" for k, v in sorted(totals.items())]
    out += ["", "## What the scorer said about each fault (probes)", "", "| probe | count |", "|---|---:|"]
    out += [f"| {k} | {v} |" for k, v in sorted(probes.items())]
    return "\n".join(out) + "\n"


def main(argv: list[str]) -> int:
    if len(argv) < 3 or argv[1] not in ("cost", "pairs"):
        print(__doc__)
        return 2
    dirs = [Path(a) for a in argv[2:]]
    print((cost if argv[1] == "cost" else pairs)(dirs))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
