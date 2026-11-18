"""What each combination of the two safety switches would have decided for one incident (the website's "transparency mode").

Pure functions, nothing here influences a decision: the engine decides with its real settings, and this only works out, for the record, what
the other three combinations would have done with the same alerts and the same second opinion.

    rules only      no second opinion, no ambiguity rule
    + second opinion   a disagreeing opinion escalates instead of acting
    + ambiguity rule   two causes' deciding alerts at once escalates instead of acting on the deepest
    both
"""
from __future__ import annotations

from correlate import ambiguous_causes, correlate, why_unmatched

COMBINATIONS = (
    ("rules", "Rules only", False, False),
    ("ml", "Rules + second opinion", True, False),
    ("amb", "Rules + ambiguity rule", False, True),
    ("both", "Rules + both", True, True),
)


def _decide(firing: list, rules_result: dict | None, verdict: str | None, use_ml: bool, use_amb: bool) -> dict:
    result = correlate(firing, use_amb)
    if result is None:
        return {"action": "escalate", "cause": None, "playbook": None, "reason": why_unmatched(firing, use_amb)}
    if use_ml and verdict == "disagree":
        return {"action": "escalate", "cause": result["root_cause"], "playbook": None,
                "reason": f"the rules said {result['root_cause']} but the second opinion disagrees"}
    if not use_ml:
        why = "decided by the rules alone"
    elif verdict is None:
        why = "no second opinion was consulted"
    else:
        why = f"the second opinion said {verdict}"
    return {"action": "act", "cause": result["root_cause"], "playbook": result.get("playbook"), "reason": why}


def build_variants(firing: list, rules_result: dict | None, opinion: dict | None, active_mode: str, active_ambiguity: bool) -> dict:
    """`rules_result` is correlate(firing, False); `opinion` is the second opinion on it (or None when it was not consulted)."""
    verdict = opinion.get("verdict") if opinion else None
    rows = []
    for key, label, use_ml, use_amb in COMBINATIONS:
        row = _decide(firing, rules_result, verdict, use_ml, use_amb)
        row.update(key=key, label=label, use_second_opinion=use_ml, use_ambiguity=use_amb,
                   active=(use_ml == (active_mode == "gate")) and (use_amb == bool(active_ambiguity)))
        rows.append(row)
    return {"second_opinion_verdict": verdict or "not consulted", "ambiguous_causes": ambiguous_causes(firing), "rows": rows}
