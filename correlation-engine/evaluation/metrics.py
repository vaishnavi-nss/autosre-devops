"""Metrics for judging an incident classifier, written for an operations setting.

Plain accuracy hides the distinction that matters here. When the engine is
wrong, the two failure modes are not equally bad:

    ABSTAIN  the engine says "I don't know" and a human takes over. Costs time.
    WRONG    the engine names the wrong cause and runs the wrong playbook.
             Costs time AND can make the incident worse.

So outcomes are split into four, and the headline number is the WRONG rate.

    correct action       named the right cause
    correct escalation   there was no known cause, and it escalated
    missed (abstain)     there was a known cause, but it escalated
    wrong action         named a cause that was not the true one - including
                         naming a cause when the truth was "no known cause"

Pure stdlib: this module is used by code that must run without numpy.
"""

from __future__ import annotations

from collections import Counter

ESCALATE = "escalate"


def norm(label: str | None) -> str:
    """Map a cause label (or None) to a class name."""
    return label if label else ESCALATE


def classes(*label_lists: list[str]) -> list[str]:
    """Every class seen, causes sorted alphabetically with `escalate` last."""
    seen = {c for labels in label_lists for c in labels}
    return sorted(c for c in seen if c != ESCALATE) + ([ESCALATE] if ESCALATE in seen else [])


def outcome(truth: str, pred: str) -> str:
    if pred == truth:
        return "correct_escalation" if truth == ESCALATE else "correct_action"
    if pred == ESCALATE:
        return "missed"
    return "wrong_action"


def outcome_counts(truths: list[str], preds: list[str]) -> Counter:
    return Counter(outcome(t, p) for t, p in zip(truths, preds))


def accuracy(truths: list[str], preds: list[str]) -> float:
    return sum(t == p for t, p in zip(truths, preds)) / len(truths) if truths else 0.0


def confusion(truths: list[str], preds: list[str]) -> Counter:
    return Counter(zip(truths, preds))


def per_class(truths: list[str], preds: list[str]) -> dict[str, dict[str, float]]:
    """Precision, recall, F1 and support for each class."""
    out: dict[str, dict[str, float]] = {}
    for c in classes(truths, preds):
        tp = sum(t == c and p == c for t, p in zip(truths, preds))
        fp = sum(t != c and p == c for t, p in zip(truths, preds))
        fn = sum(t == c and p != c for t, p in zip(truths, preds))
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        out[c] = {"precision": precision, "recall": recall, "f1": f1, "support": tp + fn}
    return out


def gate_table(
    truths: list[str],
    preds: list[str],
    confidences: list[float],
    gates: tuple[float, ...] = (0.0, 0.5, 0.75, 0.9),
) -> list[dict]:
    """What a minimum-confidence gate buys, as the dispatcher would apply it.

    An action is taken only if the engine named a cause AND its confidence is at
    least the gate; otherwise the incident escalates. For each gate: how many
    incidents get an automated action, how often that action is wrong, and what
    share of real, known-cause incidents get remediated automatically.
    """
    rows = []
    n = len(truths)
    known = sum(t != ESCALATE for t in truths)
    for gate in gates:
        taken = [
            (t, p)
            for t, p, c in zip(truths, preds, confidences)
            if p != ESCALATE and c >= gate
        ]
        right = sum(t == p for t, p in taken)
        wrong = len(taken) - right
        rows.append(
            {
                "gate": gate,
                "actions": len(taken),
                "action_rate": len(taken) / n if n else 0.0,
                "wrong_actions": wrong,
                "wrong_rate": wrong / n if n else 0.0,
                "precision": right / len(taken) if taken else 0.0,
                "auto_remediated": right / known if known else 0.0,
            }
        )
    return rows


def pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def render_outcomes(counts: Counter, n: int) -> str:
    rows = [
        ("correct action", "correct_action"),
        ("correct escalation", "correct_escalation"),
        ("missed (escalated a known cause)", "missed"),
        ("WRONG ACTION", "wrong_action"),
    ]
    lines = ["| outcome | count | share |", "|---|---:|---:|"]
    for title, key in rows:
        lines.append(f"| {title} | {counts.get(key, 0)} | {pct(counts.get(key, 0) / n if n else 0)} |")
    return "\n".join(lines)


def render_confusion(truths: list[str], preds: list[str]) -> str:
    cm = confusion(truths, preds)
    cols = classes(truths, preds)
    short = {c: c.replace("_", " ") for c in cols}
    lines = [
        "| true \\ predicted | " + " | ".join(short[c] for c in cols) + " |",
        "|---|" + "|".join("---:" for _ in cols) + "|",
    ]
    for t in classes(truths):
        lines.append(f"| **{short[t]}** | " + " | ".join(str(cm.get((t, p), 0)) for p in cols) + " |")
    return "\n".join(lines)


def render_per_class(truths: list[str], preds: list[str]) -> str:
    lines = ["| class | precision | recall | F1 | support |", "|---|---:|---:|---:|---:|"]
    for c, m in per_class(truths, preds).items():
        lines.append(
            f"| {c} | {pct(m['precision'])} | {pct(m['recall'])} | {pct(m['f1'])} | {int(m['support'])} |"
        )
    return "\n".join(lines)


def render_gate_table(rows: list[dict]) -> str:
    lines = [
        "| min confidence | automated actions | wrong actions | precision of actions "
        "| known incidents auto-remediated |",
        "|---:|---:|---:|---:|---:|",
    ]
    for r in rows:
        lines.append(
            f"| {r['gate']:.2f} | {r['actions']} ({pct(r['action_rate'])}) "
            f"| {r['wrong_actions']} ({pct(r['wrong_rate'])}) "
            f"| {pct(r['precision'])} | {pct(r['auto_remediated'])} |"
        )
    return "\n".join(lines)
