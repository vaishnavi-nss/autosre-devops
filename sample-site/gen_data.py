"""Generates data.js for the static sample website: one year of SAMPLE incidents, decisions, playbook runs and setting changes.

Everything here is invented for demonstration (seeded, so the file is reproducible). It follows the real engine and dispatcher rules
(signature weights, the 0.75-style confidence gate, shadow/gate second opinion, the ambiguity rule, cooldown, verification) but none of it
happened on a real system, and the site says so on every page.

    python sample-site/gen_data.py      ->  sample-site/data.js
"""
from __future__ import annotations

import datetime as dt
import json
import random
from pathlib import Path

R = random.Random(20261118)
UTC = dt.timezone.utc
END = dt.datetime(2026, 11, 18, 12, 0, tzinfo=UTC)            # the sample's "now"
START = END - dt.timedelta(days=365)

SIG = {
    "db_connection_pool_exhaustion": dict(owner="transaction-service", required="DBPoolNearExhaustion", playbook="restart_service.yml",
                                          weights={"DBPoolNearExhaustion": .55, "ErrorRateHigh": .20, "PodRestartsHigh": .15, "APILatencyHigh": .10}),
    "high_memory_usage": dict(owner="transaction-service", required="ContainerMemoryHigh", playbook="scale_replicas.yml",
                              weights={"ContainerMemoryHigh": .60, "PodRestartsHigh": .25, "APILatencyHigh": .15}),
    "db_disk_full": dict(owner="database", required="DBDiskNearFull", playbook="clear_logs.yml",
                         weights={"DBDiskNearFull": .60, "ErrorRateHigh": .25, "APILatencyHigh": .15}),
}
ALERT_SERVICE = {"DBPoolNearExhaustion": "transaction-service", "ContainerMemoryHigh": "transaction-service", "DBDiskNearFull": "database",
                 "ErrorRateHigh": "api-gateway", "PodRestartsHigh": "transaction-service", "APILatencyHigh": "api-gateway"}
PEOPLE = ["a.rao", "j.kim", "s.okafor", "m.silva", "r.menon", "l.chen", "t.novak", "p.iyer"]
PLAY_SECONDS = {"restart_service.yml": (24, 36), "scale_replicas.yml": (21, 31), "clear_logs.yml": (20, 29)}

# ------------------------------------------------------------------------------ setting changes over the year (day offsets from START)
EVENTS = [
    (0, "live_remediation", "off", "Initial rollout: decisions are recorded only", "platform"),
    (0, "confidence_threshold", "0.80", "Conservative starting threshold", "platform"),
    (0, "second_opinion", "not deployed", "ML service not deployed yet", "platform"),
    (0, "ambiguity_rule", "off", "Default", "platform"),
    (60, "second_opinion", "shadow", "ML second opinion deployed; records its verdict, never changes a decision", "r.menon"),
    (75, "live_remediation", "on", "Approved after 60 days of dry runs with no wrong diagnosis: restart/scale/clear-logs may run", "r.menon"),
    (120, "confidence_threshold", "0.75", "Lone deciding symptoms (0.55-0.60) still escalate; 0.75 admits the two-symptom cases that were always correct", "j.kim"),
    (200, "ambiguity_rule", "on", "Evaluation: escalate when two known causes' deciding alerts fire together", "j.kim"),
    (230, "ambiguity_rule", "off", "Evaluation ended: it escalated 14 incidents the rules could have fixed; two-fault cases stay a risk (see review notes)", "j.kim"),
    (300, "second_opinion", "gate", "Trial: a disagreeing second opinion escalates instead of acting", "s.okafor"),
    (312, "second_opinion", "shadow", "Trial ended: it withheld two correct fixes during a latency storm", "s.okafor"),
    (340, "playbook", "scale_replicas v2", "Memory fix now replaces the pods after scaling out (a scale-out alone left the leak)", "a.rao"),
]
EVENT_ROWS = []
for day, key, val, why, who in EVENTS:
    EVENT_ROWS.append({"t": (START + dt.timedelta(days=day, hours=R.randint(9, 16), minutes=R.randint(0, 59))).isoformat(), "setting": key, "value": val, "reason": why, "by": who})
EVENT_ROWS.sort(key=lambda e: e["t"])


def settings_at(t: dt.datetime) -> dict:
    cur = {}
    for e in EVENT_ROWS:
        if dt.datetime.fromisoformat(e["t"]) <= t:
            cur[e["setting"]] = e["value"]
    return {"live": cur.get("live_remediation") == "on", "threshold": float(cur.get("confidence_threshold", "0.80")),
            "second_opinion": cur.get("second_opinion", "not deployed"), "ambiguity": cur.get("ambiguity_rule") == "on"}


# ------------------------------------------------------------------------------ arrival times
def arrivals() -> list[dt.datetime]:
    out = []
    storm_days = set(R.sample(range(365), 6))
    for d in range(365):
        day = START + dt.timedelta(days=d)
        base = 1.6 + (0.7 if day.weekday() < 5 else -0.5) + (1.2 if day.day >= 27 else 0)         # month-end batch pressure
        if d in storm_days:
            base += R.uniform(5, 9)
        n = max(0, int(R.gauss(base, 1.1)))
        for _ in range(n):
            hour = R.choices(range(24), weights=[2, 1, 1, 1, 1, 2, 3, 5, 8, 9, 9, 8, 7, 8, 9, 9, 8, 7, 6, 5, 4, 3, 3, 2])[0]
            out.append(day.replace(hour=hour, minute=R.randint(0, 59), second=R.randint(0, 59)))
    out.sort()
    return out


# ------------------------------------------------------------------------------ decision logic (mirrors engine + dispatcher)
def confidence(cause: str, fired: set[str]) -> float:
    w = SIG[cause]["weights"]
    return round(sum(v for k, v in w.items() if k in fired), 2)


def decide(cause_list, deepest_alerts, ml_verdict, st, in_cooldown):
    """Return (named_cause, action, reason). Follows engine.receive_alerts, then the dispatcher."""
    if not cause_list:
        return None, "escalated", "no known cause fits these symptoms"
    if st["ambiguity"] and len(cause_list) > 1:
        return None, "escalated", f"the deciding symptoms of {len(cause_list)} causes fired together ({', '.join(cause_list)})"
    cause = cause_list[0]
    if st["second_opinion"] == "gate" and ml_verdict == "disagree":
        return cause, "vetoed", f"the rules said {cause} but the metrics disagree"
    return cause, "ok", ""


def variants(cause_list, ml_verdict, st, playbooks):
    rows = []
    for key, label, use_ml, use_amb in (("rules", "Rules only", False, False), ("ml", "Rules + second opinion", True, False),
                                         ("amb", "Rules + ambiguity rule", False, True), ("both", "Rules + both", True, True)):
        if not cause_list:
            r = dict(action="escalate", cause=None, reason="no known cause fits these symptoms")
        elif use_amb and len(cause_list) > 1:
            r = dict(action="escalate", cause=None, reason=f"the deciding symptoms of {len(cause_list)} causes fired together")
        elif use_ml and ml_verdict == "disagree":
            r = dict(action="escalate", cause=cause_list[0], reason="the second opinion disagrees with the rules")
        else:
            r = dict(action="act", cause=cause_list[0], playbook=playbooks.get(cause_list[0]),
                     reason=("the second opinion said " + ml_verdict) if use_ml and ml_verdict else "decided by the rules alone")
        r.update(key=key, label=label, active=(use_ml == (st["second_opinion"] == "gate")) and (use_amb == st["ambiguity"]))
        rows.append(r)
    return rows


NOTES = {
    "db_connection_pool_exhaustion": ["Pool leak after a slow downstream query; restarted the pod and raised the query timeout ticket", "Connections held by a stuck batch job; killed the job, pool recovered",
                                       "Restart cleared it; root cause is a missing close() in the statement path (ticket raised)"],
    "high_memory_usage": ["Heap growth after the new statement cache; rolled back the build", "Scaled out and replaced pods; leak traced to unbounded session map",
                           "Raised the memory limit as a stopgap; follow-up ticket for the leak"],
    "db_disk_full": ["Archived old transaction logs and extended the volume", "Log rotation had silently failed; fixed the cron and cleared logs", "Cleared logs; volume resize scheduled"],
    None: ["Network partition between gateway and transaction service; network team engaged", "CPU throttling on the gateway after a noisy neighbour; moved the pod", "Pod crash loop from a bad config map; reverted the config",
           "Upstream partner timeout, not our fault; no action", "Transient: alerts cleared by themselves before anyone acted"],
}


def build() -> dict:
    incs = []
    last_fix: dict[str, dt.datetime] = {}
    n = 0
    for t0 in arrivals():
        st = settings_at(t0)
        kind = R.choices(["pool", "memory", "disk", "unknown", "two", "lone"], weights=[30, 27, 13, 14, 9, 7])[0]
        fired: set[str] = set()
        causes: list[str] = []
        if kind == "pool":
            causes = ["db_connection_pool_exhaustion"]; fired = {"DBPoolNearExhaustion"} | set(R.sample(["ErrorRateHigh", "PodRestartsHigh", "APILatencyHigh"], R.choice([1, 2, 2, 3])))
        elif kind == "memory":
            causes = ["high_memory_usage"]; fired = {"ContainerMemoryHigh"} | set(R.sample(["PodRestartsHigh", "APILatencyHigh"], R.choice([1, 1, 2])))
        elif kind == "disk":
            causes = ["db_disk_full"]; fired = {"DBDiskNearFull"} | set(R.sample(["ErrorRateHigh", "APILatencyHigh"], R.choice([1, 2, 2])))
        elif kind == "lone":
            c = R.choice(list(SIG)); causes = [c]; fired = {SIG[c]["required"]}
        elif kind == "two":
            a, b = R.sample(list(SIG), 2); causes = sorted([a, b]); fired = {SIG[a]["required"], SIG[b]["required"]} | {"APILatencyHigh"}
        else:
            fired = set(R.sample(["ErrorRateHigh", "APILatencyHigh", "PodRestartsHigh"], R.choice([1, 2])))
        # 'deepest' cause is the one named when the ambiguity rule is off: database before transaction-service
        order = {"db_disk_full": 0, "db_connection_pool_exhaustion": 1, "high_memory_usage": 2}
        causes = sorted(causes, key=lambda c: order[c])
        n += 1
        iid = f"INC-{1000 + n}"
        service = SIG[causes[0]]["owner"] if causes else R.choice(["api-gateway", "api-gateway", "transaction-service", "frontend"])
        named_pre = causes[0] if causes else None
        conf = confidence(named_pre, fired) if named_pre else None
        # second opinion
        ml = None
        if st["second_opinion"] != "not deployed" and causes:
            ml = R.choices(["agree", "disagree", "unavailable", "no_opinion"], weights=[88, 4 if causes[0] != "high_memory_usage" else 7, 3, 5])[0]
        ev = []
        alert_names = sorted(fired)
        recv = t0 + dt.timedelta(seconds=R.randint(48, 110))
        ev.append({"t": t0.isoformat(), "k": "alert", "text": f"Prometheus fired {len(alert_names)} alert(s): {', '.join(alert_names)}"})
        ev.append({"t": recv.isoformat(), "k": "group", "text": "Alertmanager sent one grouped incident to the correlation engine"})
        named, action_gate, why = decide(causes, alert_names, ml, st, False)
        action = None; playbook = None; reason = why; dur = None; verified = None; outcome = None; ttr = None
        t = recv + dt.timedelta(seconds=2)
        if named:
            ev.append({"t": t.isoformat(), "k": "diag", "text": f"Root cause: {named} on {service}, confidence {int(conf * 100)}%"})
        else:
            ev.append({"t": t.isoformat(), "k": "diag", "text": "No cause named: " + (why or "no known cause")})
        if ml and named_pre:
            ev.append({"t": (t + dt.timedelta(seconds=1)).isoformat(), "k": "ml", "text": f"ML second opinion: {ml}"})
        if named is None:
            action = "escalated"
        elif action_gate == "vetoed":
            action = "vetoed"; reason = why
        elif conf < st["threshold"]:
            action = "escalated"; reason = f"confidence {int(conf * 100)}% is below the {int(st['threshold'] * 100)}% threshold"
        elif not st["live"]:
            action = "dry_run"; playbook = SIG[named]["playbook"]; reason = "live remediation was off; nothing was run"
        elif last_fix.get(named) and (recv - last_fix[named]).total_seconds() < 600:
            action = "skipped"; playbook = SIG[named]["playbook"]; reason = "cooldown: the same fix ran less than 10 minutes ago"
        else:
            playbook = SIG[named]["playbook"]
            r = R.random()
            lo, hi = PLAY_SECONDS[playbook]
            dur = round(R.uniform(lo, hi) + (8 if playbook == "scale_replicas.yml" and settings_at(t0)["live"] and t0 >= START + dt.timedelta(days=340) else 0), 1)
            t1 = t + dt.timedelta(seconds=3)
            ev.append({"t": t1.isoformat(), "k": "gate", "text": f"Dispatcher gate passed: confidence {int(conf * 100)}% >= {int(st['threshold'] * 100)}%, cooldown clear, {playbook} on the allow-list"})
            ev.append({"t": (t1 + dt.timedelta(seconds=1)).isoformat(), "k": "step", "text": f"preflight ok; ansible {playbook} started"})
            tend = t1 + dt.timedelta(seconds=dur)
            if r < 0.93:
                action = "auto_fixed"; verified = True
                ev.append({"t": tend.isoformat(), "k": "step", "text": f"playbook succeeded in {dur} s"})
                ev.append({"t": (tend + dt.timedelta(seconds=R.randint(8, 22))).isoformat(), "k": "ok", "text": "verified: Deployment rollout complete and replicas ready"})
                last_fix[named] = recv
                ttr = int(dur + R.randint(60, 220))
            elif r < 0.97:
                action = "escalated"; verified = False; reason = "the fix ran but recovery could not be verified"
                ev.append({"t": tend.isoformat(), "k": "bad", "text": "verification failed: " + R.choice(["rollout did not complete in time", "replicas not ready", "alerts still firing"])})
                if playbook == "scale_replicas.yml":
                    ev.append({"t": (tend + dt.timedelta(seconds=4)).isoformat(), "k": "bad", "text": "rolled back the scale-out"})
                last_fix[named] = recv
            else:
                action = "escalated"; verified = False; reason = "the playbook failed"
                ev.append({"t": tend.isoformat(), "k": "bad", "text": "ansible_failed: " + R.choice(["kubectl timed out", "forbidden by RBAC", "target deployment not found"])})
        # humans
        assignee = ack = None
        if action in ("escalated", "vetoed"):
            assignee = R.choice(PEOPLE)
            night = t0.hour < 7 or t0.hour >= 20
            ack_min = R.randint(6, 28) if night else R.randint(2, 11)
            res_min = R.randint(35, 160) if night else R.randint(14, 75)
            ack = (recv + dt.timedelta(minutes=ack_min))
            ev.append({"t": ack.isoformat(), "k": "human", "text": f"Acknowledged by {assignee}"})
            note = R.choice(NOTES[named if named else None]) if (named or not causes) else R.choice(NOTES[None])
            if action == "vetoed" and named:
                note = "Gate held the fix; engineer ran the same playbook manually after checking the metrics"
            ev.append({"t": (recv + dt.timedelta(minutes=res_min)).isoformat(), "k": "human", "text": f"Resolved by {assignee}: {note}"})
            ttr = res_min * 60
            outcome = note
        # review flags (found afterwards)
        review = None
        if action == "auto_fixed" and ml == "disagree" and R.random() < 0.18:
            review = "Post-incident review: the metrics were right; the rules named the wrong cause and the fix did not help"
        if action == "dry_run":
            ttr = None
        sev = "SEV2" if (kind in ("two",) or (named == "db_disk_full" and action != "auto_fixed")) else "SEV3"
        incs.append({
            "id": iid, "t": t0.isoformat(), "received": recv.isoformat(), "service": service, "cause": named or (causes[0] if causes else None), "named": named,
            "kind": kind, "confidence": conf, "alerts": [{"name": a, "service": ALERT_SERVICE[a]} for a in alert_names],
            "matched": sorted(a for a in alert_names if named and a in SIG[named]["weights"]), "missing": sorted(k for k in (SIG[named]["weights"] if named else {}) if k not in fired),
            "ml": ml, "action": action, "reason": reason, "playbook": playbook, "duration_s": dur, "verified": verified, "ttr_s": ttr, "sev": sev,
            "assignee": assignee, "settings": st, "ambiguous": causes if len(causes) > 1 else [], "review": review,
            "variants": variants(causes, ml, st, {c: SIG[c]["playbook"] for c in SIG}), "events": sorted(ev, key=lambda e: e["t"]),
            "note": outcome,
        })
    # make the most recent escalations still open, so the 'needs you' list has real content (shifted in time, nothing else changed)
    tail = [i for i in incs[-150:] if i["action"] in ("escalated", "vetoed")]
    pick = lambda pred: next((i for i in reversed(tail) if pred(i)), None)
    esc = [x for x in (pick(lambda i: i["named"] is None), pick(lambda i: i["verified"] is False), pick(lambda i: i["ambiguous"] or i["action"] == "vetoed" or i["ml"] == "disagree")) if x]
    esc += [i for i in reversed(tail) if i not in esc][: 3 - len(esc)]
    esc = esc[:3]
    for inc, minutes, acked in zip(esc, (22, 64, 131), (False, True, True)):
        delta = (END - dt.timedelta(minutes=minutes)) - dt.datetime.fromisoformat(inc["received"])
        for key in ("t", "received"):
            inc[key] = (dt.datetime.fromisoformat(inc[key]) + delta).isoformat()
        for e in inc["events"]:
            e["t"] = (dt.datetime.fromisoformat(e["t"]) + delta).isoformat()
        inc["events"] = [e for e in inc["events"] if not e["text"].startswith("Resolved by") and (acked or not e["text"].startswith("Acknowledged"))]
        inc["ttr_s"] = None; inc["note"] = None; inc["open"] = True; inc["assignee"] = inc["assignee"] if acked else None
        inc["sev"] = "SEV2" if minutes == 22 else inc["sev"]
    incs.sort(key=lambda i: i["t"])
    for k, inc in enumerate(incs, 1):
        inc["id"] = f"INC-{1000 + k}"
    return {"generated": END.isoformat(), "start": START.isoformat(), "incidents": incs, "events": EVENT_ROWS, "people": PEOPLE}


if __name__ == "__main__":
    data = build()
    out = Path(__file__).with_name("data.js")
    out.write_text("// SAMPLE DATA: invented for demonstration, generated by gen_data.py. Nothing here happened on a real system.\nwindow.SAMPLE = " + json.dumps(data, separators=(",", ":")) + ";\n", encoding="utf-8")
    inc = data["incidents"]
    from collections import Counter
    print(len(inc), "incidents;", out.stat().st_size // 1024, "KB")
    print(Counter(i["action"] for i in inc))
    print(Counter(i["kind"] for i in inc))
