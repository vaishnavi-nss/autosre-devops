"""Turns what the engine and the dispatcher recorded into the shape the website's pages use. Pure functions: nothing here decides anything.

An incident (engine) and, when a playbook ran, its execution (dispatcher, with timestamped steps) become one record with a timeline of stage
events. Every stage event carries a time and a state, so the same timeline drives both the live tracker (events up to now) and the replay of a
finished incident (events up to a moving virtual clock).

Stages:  1 fault noticed   2 alerts grouped   3 root cause diagnosed   4 ML second opinion   5 dispatcher gate   6 playbook   7 recovery verified   8 back to normal
"""
from __future__ import annotations

from datetime import datetime, timezone

STAGES = ("Fault noticed", "Alerts grouped", "Root cause diagnosed", "ML second opinion", "Dispatcher safety gate", "Ansible playbook", "Recovery verified", "Back to normal")
GO = ("accepted", "ready", "dry_run", "running", "succeeded")


def parse(ts) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat(timespec="seconds") if dt else None


def _action(inc: dict, ex: dict | None) -> tuple[str, str]:
    """(action, reason). action is one of auto_fixed | running | escalated | vetoed | dry_run | skipped | decision."""
    rem = inc.get("remediation") or {}
    status = inc.get("status")
    if status == "vetoed":
        return "vetoed", inc.get("reason") or "the second opinion disagreed"
    if status == "unmatched" or inc.get("action") == "escalate_to_human" and not inc.get("root_cause"):
        return "escalated", inc.get("reason") or "no known cause fits these symptoms"
    rs = rem.get("status")
    if rs == "dry_run":
        return "dry_run", rem.get("reason") or "live remediation is off; nothing was run"
    if rs == "skipped":
        return "skipped", rem.get("reason") or "skipped"
    if rs == "escalated":
        return "escalated", rem.get("reason") or "the dispatcher escalated"
    if rs == "accepted":
        if ex is None:
            return "running", "the playbook was accepted"
        st = ex.get("status")
        if st == "succeeded":
            return "auto_fixed", ""
        if st == "running":
            return "running", ""
        return "escalated", ex.get("reason") or f"the run ended as {st}"
    return "decision", rem.get("reason") or "the dispatcher was not consulted"


def _events(inc: dict, ex: dict | None, action: str) -> list[dict]:
    recv = parse(inc.get("received_at"))
    start = parse(inc.get("started_at")) or recv
    alerts = [a.get("alertname") for a in inc.get("alerts") or [] if a.get("alertname")]
    ev: list[dict] = []

    def add(stage: int, state: str, when: datetime | None, text: str) -> None:
        ev.append({"stage": stage, "state": state, "t": _iso(when), "text": text})

    add(1, "done", start, f"Prometheus raised {len(alerts)} alert(s): {', '.join(sorted(set(alerts)))}" if alerts else "An alert fired")
    add(2, "done", recv, "Alertmanager sent the alerts that arrived together as one incident to the correlation engine")
    cause = inc.get("root_cause")
    if cause:
        conf = inc.get("confidence")
        add(3, "done", recv, f"{cause} on {inc.get('service') or '?'}, confidence {round((conf or 0) * 100)}%" + (f" (matched: {', '.join(inc['matched_symptoms'])})" if inc.get("matched_symptoms") else ""))
    else:
        add(3, "stop", recv, "No known cause fits these symptoms" + (f": {inc['reason']}" if inc.get("reason") else ""))
    so = inc.get("second_opinion")
    if cause and so:
        v = so.get("verdict")
        add(4, "stop" if v == "disagree" else ("done" if v == "agree" else "warn"), recv,
            f"ML says {v}" + (f": metrics point at {so.get('scorer_fault')}" + (f" on {so.get('scorer_service')}" if so.get("scorer_service") else "") if so.get("scorer_fault") else ""))
    elif cause:
        add(4, "na", recv, "The second opinion was not consulted")
    rem = inc.get("remediation")
    if action == "vetoed":
        add(5, "stop", recv, "Held back: " + (inc.get("reason") or "the second opinion disagreed"))
    elif not cause:
        add(5, "stop", recv, "Escalated to a person; nothing was run")
    elif rem:
        ok = rem.get("status") in GO
        add(5, "done" if ok else "stop", recv, "Checked confidence, second opinion, cooldown and the allow-list: " + str(rem.get("status")) + (f" ({rem['reason']})" if rem.get("reason") else ""))
    if ex:
        steps = {s.get("step"): parse(s.get("t")) for s in ex.get("steps") or []}
        begin = steps.get("preflight") or parse(ex.get("started_at"))
        add(6, "start", begin, f"{ex.get('playbook')} started")
        if "ansible_failed" in steps or "ansible_timeout" in steps:
            add(6, "stop", steps.get("ansible_failed") or steps.get("ansible_timeout"), f"{ex.get('playbook')} failed")
        elif "verify" in steps:
            add(6, "done", steps["verify"], f"{ex.get('playbook')} finished" + (f" in {ex['duration_s']} s" if ex.get("duration_s") is not None else ""))
            add(7, "start", steps["verify"], "Reading the Deployment: rollout complete, replicas ready?")
            if "recovered" in steps:
                add(7, "done", steps["recovered"], "Verified: the Deployment itself shows recovery, not just the playbook's exit code")
                add(8, "done", parse(ex.get("finished_at")) or steps["recovered"], "Recovery confirmed")
            elif "verification_failed" in steps:
                add(7, "stop", steps["verification_failed"], "Recovery could not be verified; escalated")
        elif ex.get("status") not in ("running", None):
            add(6, "stop", parse(ex.get("finished_at")), f"{ex.get('playbook')} ended as {ex.get('status')}")
        if "rollback" in steps:
            add(7, "stop", steps["rollback"], "Rolled back the scale-out")
    ev.sort(key=lambda e: (e["t"] or "", e["stage"]))
    return ev


def normalize(inc: dict, executions: dict[str, dict], acks: dict[str, dict], resolved: dict[str, dict], evidence: dict[str, dict] | None = None) -> dict:
    rem = inc.get("remediation") or {}
    ex = executions.get(rem.get("execution_id")) if rem.get("execution_id") else None
    action, reason = _action(inc, ex)
    recv, start = parse(inc.get("received_at")), parse(inc.get("started_at")) or parse(inc.get("received_at"))
    fin = parse((ex or {}).get("finished_at"))
    ttr = int((fin - start).total_seconds()) if (action == "auto_fixed" and fin and start) else None
    iid = inc.get("id") or "INC-" + str(abs(hash(inc.get("received_at"))))[:6]
    ack, res = acks.get(iid), resolved.get(iid)
    needs_human = action in ("escalated", "vetoed")
    if res and recv:
        ttr = int((parse(res["t"]) - start).total_seconds()) if parse(res.get("t")) and start else ttr
    so = inc.get("second_opinion") or {}
    cause = inc.get("root_cause")
    events = _events(inc, ex, action)
    for e in ([ack] if ack else []) + ([res] if res else []):
        events.append({"stage": 0, "state": "human", "t": e.get("t"), "text": ("Acknowledged by " + str(e.get("by"))) if e is ack else ("Resolved by " + str(e.get("by")) + (": " + str(e.get("text")) if e.get("text") else ""))})
    events.sort(key=lambda e: (e["t"] or "", e["stage"]))
    return {
        "id": iid, "t": _iso(start), "received": _iso(recv), "service": inc.get("service") or (inc["alerts"][0].get("service") if inc.get("alerts") else None),
        "cause": cause, "confidence": inc.get("confidence"), "alerts": [{"name": a.get("alertname"), "service": a.get("service")} for a in inc.get("alerts") or []],
        "matched": inc.get("matched_symptoms") or [], "missing": inc.get("missing_symptoms") or [], "ml": so.get("verdict"), "action": action, "reason": reason,
        "playbook": inc.get("playbook") or (ex or {}).get("playbook"), "duration_s": (ex or {}).get("duration_s"), "verified": (True if action == "auto_fixed" else None),
        "ttr_s": ttr, "sev": "SEV2" if needs_human else "SEV3", "assignee": (ack or {}).get("by"), "open": bool(needs_human and not res),
        "variants": inc.get("variants"), "ambiguous": (inc.get("variants") or {}).get("ambiguous_causes") or [], "explanation": inc.get("explanation"),
        "events": events, "note": (res or {}).get("text"), "execution_id": rem.get("execution_id"),
        "evidence": _evidence((inc.get("diagnostics") or {}).get("execution_id"), evidence or {}),
    }


def _evidence(ref, collected: dict[str, dict]) -> dict | None:
    """What the dispatcher gathered for a person to read (pod states, events, log tails), or the fact that it is still being gathered."""
    rec = collected.get(ref) if ref else None
    if not rec:
        return None
    return {"status": rec.get("status"), "service": rec.get("target_service"), "at": rec.get("finished_at") or rec.get("started_at"), "data": rec.get("diagnostics"), "reason": rec.get("reason")}


PARTIAL_WINDOW_S = 600


def mark_partial(incs: list[dict]) -> None:
    """Flag the early partial notifications: an incident with no named cause whose alerts overlap (same alert name or same service) with an incident
    that does have a cause, received within ten minutes. Alertmanager can post a group before its deciding alert has fired, or a group of stray
    alerts at the same moment as a real fault; the later or sibling incident carries the diagnosis, so the partial one is only noise for an on-call
    engineer. Sets `partial` (True/False) and `superseded_by` (the id of the incident that carries the diagnosis)."""
    named = [i for i in incs if i.get("cause")]
    for i in incs:
        i["partial"], i["superseded_by"] = False, None
        if i.get("cause") or i.get("action") != "escalated":
            continue
        r = parse(i.get("received"))
        names = {a["name"] for a in i.get("alerts") or []}
        for b in named:
            rb = parse(b.get("received"))
            if not r or not rb or abs((rb - r).total_seconds()) > PARTIAL_WINDOW_S:
                continue
            if names & {a["name"] for a in b.get("alerts") or []} or (i.get("service") and i.get("service") == b.get("service")):
                i["partial"], i["superseded_by"] = True, b["id"]
                break


def build(incidents: list[dict], executions: list[dict], events: list[dict], diagnostics: list[dict] | None = None) -> dict:
    """The website's history: normalised incidents (newest first), setting changes and scenario runs (newest first)."""
    by_exec = {e.get("id"): e for e in executions}
    acks: dict[str, dict] = {}
    resolved: dict[str, dict] = {}
    for e in sorted(events, key=lambda x: x.get("t") or ""):                   # later records win
        if e.get("kind") == "ack" and e.get("name"):
            acks[e["name"]] = {"by": e.get("by"), "t": e.get("t")}
        elif e.get("kind") == "resolve" and e.get("name"):
            resolved[e["name"]] = {"by": e.get("by"), "t": e.get("t"), "text": e.get("text")}
    evidence = {d.get("id"): d for d in diagnostics or []}
    inc = [normalize(i, by_exec, acks, resolved, evidence) for i in incidents]
    inc.sort(key=lambda i: (i["t"] or "", i["received"] or ""), reverse=True)
    mark_partial(inc)
    return {"incidents": inc, "changes": [e for e in events if e.get("kind") == "change"], "runs": [e for e in events if e.get("kind") == "run"]}
