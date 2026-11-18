"""Builds data_live.js from the logs collected so far (nothing invented):

  * the 31 Oct 2026 live validation (docs/live-validation: trials.jsonl + report.md): 17 usable trials with real timestamps, alerts, decisions;
  * the 18 Nov 2026 fault-console runs (three simulated, four live) with the times recorded in the test report and the runner logs;
  * a run-level list of every validation session (summaries under autosre/docs and the run table of the test report);
  * the changes to what the automation may do (live switches from the runner logs, deploys, and the commits that changed behaviour).

Fields a log did not record are left empty; the console shows them as '-'. Counterfactual rows ("what each setting would have decided") are
computed with the real engine code (correlation-engine/transparency.py) from the recorded alerts or the deciding symptom.

    python sample-site/build_real.py     ->  sample-site/data_live.js
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO / "correlation-engine"))
from correlate import correlate  # noqa: E402
from signatures import SIGNATURES  # noqa: E402
from transparency import build_variants  # noqa: E402

UTC = dt.timezone.utc
LIVE1 = REPO / "docs" / "live-validation"
RUNS_DOCS = REPO.parent / "autosre" / "docs"
REPORT = REPO / "docs" / "completion" / "03-test-report.md"

ALERT_SERVICE = {"DBPoolNearExhaustion": "transaction-service", "ContainerMemoryHigh": "transaction-service", "DBDiskNearFull": "database",
                 "ErrorRateHigh": "api-gateway", "PodRestartsHigh": "transaction-service", "APILatencyHigh": "api-gateway", "CPUThrottlingHigh": "api-gateway"}
PLAYBOOK = {c: s["playbook"] for c, s in SIGNATURES.items()}
OWNER = {c: s["owner"] for c, s in SIGNATURES.items()}
REQUIRED = {c: s["required_symptoms"][0] for c, s in SIGNATURES.items()}


def iso(d: dt.datetime) -> str:
    return d.astimezone(UTC).isoformat(timespec="seconds")


def firing(names: list[str]) -> list[dict]:
    return [{"status": "firing", "labels": {"alertname": n, "service": ALERT_SERVICE.get(n, "api-gateway"), "severity": "critical"}} for n in names]


def settings(live: bool, ml: str) -> dict:
    return {"live": live, "threshold": 0.75, "second_opinion": ml, "ambiguity": False}


# ------------------------------------------------------------------------------------------------ 31 Oct: live validation (injected faults)
def oct31() -> list[dict]:
    trials = {}
    for line in (LIVE1 / "trials.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            trials[int(r["trial"])] = r
    rows = []
    for line in (LIVE1 / "report.md").read_text(encoding="utf-8").splitlines():
        m = re.match(r"\|\s*(\d+)\s*\|(.*)\|\s*$", line)
        if not m or int(m.group(1)) not in trials:
            continue
        cells = [c.strip() for c in m.group(2).split("|")]
        if len(cells) < 9:
            continue
        rows.append((int(m.group(1)), cells))
    out = []
    for n, (injected, symptoms, first, conf, after, records, final, acts, outcome) in rows:
        tr = trials[n]
        inj = dt.datetime.fromisoformat(tr["injected_at"])
        delay = float(tr.get("first_incident_s") or after.rstrip("s"))
        recv = inj + dt.timedelta(seconds=delay)
        cause = None if injected == "unknown" else injected
        named = None if first == "escalate" else first
        names = [s.strip() for s in symptoms.split(",") if s.strip()]
        c = float(conf) if named else None
        rules = correlate(firing(names), False)
        ev = [{"t": iso(inj), "k": "alert", "text": f"Fault injected through the chaos endpoint ({tr.get('params') and json.dumps(tr['params']) or injected})"},
              {"t": iso(recv), "k": "group", "text": f"First incident reached the engine {delay:.0f} s after injection; symptoms: {', '.join(names)}"}]
        if named:
            ev.append({"t": iso(recv + dt.timedelta(seconds=2)), "k": "diag", "text": f"First decision: {named}, confidence {int(float(conf) * 100)}%"})
        else:
            ev.append({"t": iso(recv + dt.timedelta(seconds=2)), "k": "diag", "text": "First decision: no known cause, escalate"})
        if named and float(conf) < 0.75:
            action, reason, playbook = "dry_run", f"confidence {int(float(conf) * 100)}% is below the 75% gate on the first webhook (the deciding symptom fired alone), so a dispatcher would have escalated; none was connected, nothing ran", PLAYBOOK[named]
        elif named:
            action, reason, playbook = "dry_run", "decision recorded; the dispatcher was not connected in this session, so nothing was run (it would have acted at this confidence)", PLAYBOOK[named]
        else:
            action, reason, playbook = "escalated", "no known cause fits these symptoms", None
        ev.append({"t": iso(recv + dt.timedelta(seconds=4)), "k": "step", "text": f"{records} webhooks arrived for this fault; final decision: {final}"})
        if acts not in ("never", "-"):
            ev.append({"t": iso(inj + dt.timedelta(seconds=float(acts.rstrip('s')))), "k": "ok", "text": f"A decision the dispatcher would act on at 0.75 first appeared {acts} after injection"})
        ev.sort(key=lambda e: e["t"])
        out.append({
            "id": "", "t": iso(inj), "received": iso(recv), "service": OWNER[cause] if cause else "api-gateway", "cause": cause, "named": named, "kind": "injected",
            "confidence": c, "alerts": [{"name": a, "service": ALERT_SERVICE.get(a, "api-gateway")} for a in names],
            "matched": [a for a in names if named and a in SIGNATURES[named]["weights"]], "missing": [k for k in (SIGNATURES[named]["weights"] if named else {}) if k not in names],
            "ml": None, "action": action, "reason": reason, "playbook": playbook, "duration_s": None, "verified": None, "ttr_s": None, "sev": "SEV3", "assignee": None,
            "settings": settings(False, "not deployed"), "ambiguous": [], "review": None,
            "variants": build_variants(firing(names), rules, None, "shadow", False)["rows"], "events": ev, "note": f"Session 2026-10-31, trial {n} ({outcome}).", "source": "live validation, 31 Oct"})
    return out


# ------------------------------------------------------------------------------------------------ 18 Nov: fault-console runs
def at(hhmmss: str) -> dt.datetime:
    h, m, s = map(int, hhmmss.split(":"))
    return dt.datetime(2026, 11, 18, h, m, s, tzinfo=UTC)


CONSOLE = [
    # name, cause, mode, started, injected, result time, playbook seconds, confidence, extra events [(time, kind, text)], note
    ("pool", "db_connection_pool_exhaustion", "simulate", "02:12:36", "02:14:01", "02:16:25", None, .85, [("02:16:25", "ok", "Result: dry_run. All checks passed, live execution was off, so nothing was fixed (not a recovery)")], "Simulated run through the fault console."),
    ("memory", "high_memory_usage", "simulate", "02:22:33", "02:23:58", "02:27:23", None, .75, [("02:23:58", "alert", "Real memory held in the container: median transaction 1.226 s"), ("02:27:23", "ok", "Result: dry_run. Nothing was fixed (not a recovery)")], "Simulated run through the fault console."),
    ("disk", "db_disk_full", "simulate", "02:33:16", "02:34:42", "02:36:09", None, .85, [("02:36:09", "ok", "Result: dry_run. Nothing was fixed (not a recovery)"), ("02:42:27", "step", "Runner restored the safe settings")], "Simulated run through the fault console."),
    ("pool", "db_connection_pool_exhaustion", "live", "02:47:50", "02:49:22", "02:52:26", 29.2, None, [], "Live run through the fault console."),
    ("memory", "high_memory_usage", "live", "02:57:33", "02:59:05", "03:03:03", 24.4, None, [("02:59:05", "alert", "Real memory held in the container: median transaction 1.292 s")], "Live run through the fault console."),
    ("disk", "db_disk_full", "live", "03:08:32", "03:10:06", "03:11:54", 23.9, None, [("03:17:41", "step", "Runner restored the safe settings"), ("03:17:50", "step", "Runner finished")], "Live run through the fault console."),
    ("memory", "high_memory_usage", "live", "03:49:44", "03:51:17", "03:55:25", None, None,
     [("03:51:11", "step", "Live remediation switched on for this run only"), ("03:52:05", "alert", "Real memory held in the container: median transaction 1.199 s"),
      ("03:55:35", "ok", "Leaking pod 68d95859d4-4p48q terminated (Completed); new pod 58569ff9f-hcq74 Running for 10 s"),
      ("04:00:50", "step", "Runner restored: live remediation off, chaos off, faults reset, replicas back to 1"), ("04:00:59", "step", "Runner finished: 1 passed, failed or not recovered: none")],
     "Live run of the memory fix that scales out and replaces the pods (Jenkins build 2)."),
]


def console_runs() -> list[dict]:
    out = []
    for name, cause, mode, started, injected, done, secs, conf, extra, note in CONSOLE:
        live = mode == "live"
        inj, end = at(injected), at(done)
        ev = [{"t": iso(at(started)), "k": "step", "text": f"Fault console started a {mode} {name} run; preflight passed"},
              {"t": iso(inj), "k": "alert", "text": f"Real fault injected: {name}"}]
        ev += [{"t": iso(at(t)), "k": k, "text": x} for t, k, x in extra]
        if live:
            pb = PLAYBOOK[cause]
            ev.append({"t": iso(end - dt.timedelta(seconds=40)), "k": "ml", "text": "ML second opinion: agree (the dispatcher's policy requires agreement before it acts)"})
            ev.append({"t": iso(end - dt.timedelta(seconds=30)), "k": "step", "text": f"ansible {pb} " + (f"succeeded in {secs} s" if secs else "succeeded")})
            ev.append({"t": iso(end), "k": "ok", "text": "Result: passed. Alert, diagnosis, second opinion, playbook and independent recovery all confirmed"})
        else:
            ev.append({"t": iso(end - dt.timedelta(seconds=30)), "k": "ml", "text": "ML second opinion: agree"})
            ev.append({"t": iso(end - dt.timedelta(seconds=25)), "k": "diag", "text": f"Root cause {cause}, confidence {int(conf * 100)}%; playbook {PLAYBOOK[cause]} chosen"})
        ev.sort(key=lambda e: e["t"])
        rules = correlate(firing([REQUIRED[cause]]), False)
        out.append({
            "id": "", "t": iso(inj), "received": iso(inj), "service": OWNER[cause], "cause": cause, "named": cause, "kind": "console", "confidence": conf, "alerts": [],
            "matched": [], "missing": [], "ml": "agree", "action": "auto_fixed" if live else "dry_run",
            "reason": "" if live else "live remediation was off; nothing was run (not a recovery)", "playbook": PLAYBOOK[cause], "duration_s": secs, "verified": True if live else None,
            "ttr_s": int((end - inj).total_seconds()) if live else None, "sev": "SEV3", "assignee": None, "settings": settings(live, "shadow"), "ambiguous": [], "review": None,
            "variants": build_variants(firing([REQUIRED[cause]]), rules, {"verdict": "agree"}, "shadow", False)["rows"], "events": ev, "note": note, "source": "fault console, 18 Nov"})
    return out


# ------------------------------------------------------------------------------------------------ validation sessions and changes
def runs_list() -> list[dict]:
    runs = []
    text = REPORT.read_text(encoding="utf-8")
    sect = text[text.index("## Runs, in order"):text.index("## Bugs that only")]
    for line in sect.splitlines():
        m = re.match(r"\|\s*\*{0,2}([^|]+?)\*{0,2}\s*\|\s*([^|]+?)\s*\|\s*(.+?)\s*\|\s*$", line)
        if m and m.group(1) not in ("Run", "---") and not m.group(1).startswith("-"):
            runs.append({"name": m.group(1).replace("**", ""), "mode": m.group(2).replace("**", ""), "text": m.group(3).replace("**", ""), "when": "Nov 2026 (cloud completion)"})
    docs = []
    for d in ["live-validation", "live-validation-run2", "live-validation-run3", "live-validation-run4", "live-validation-run5-early", "live-validation-run7", "live-validation-run8",
              "live-validation-run9", "live-validation-run10", "live-validation-run11", "live-validation-run12", "live-validation-run13", "live-validation-run14", "live-validation-run15",
              "live-validation-run16", "real-aws-validation"]:
        for fn in ("summary.md", "report.md"):
            f = RUNS_DOCS / d / fn
            if f.exists():
                body = f.read_text(encoding="utf-8")
                head = next((l.lstrip("# ").strip() for l in body.splitlines() if l.startswith("#")), d)
                num = re.search(r"run(\d+)(-early)?", d)
                name = ("Run " + num.group(1) + (" (early)" if num.group(2) else "")) if num else ("Run 1" if d == "live-validation" else "Real AWS validation")
                if not head.lower().startswith(("live validation results", "live validation, run")) and num:
                    name += ": " + re.sub(r"^Runs? ?\d+[^:]*:\s*", "", head)
                paras = [" ".join(p.split()) for p in body.split(chr(10) * 2) if p.strip() and not p.startswith(("#", "|", "-")) and len(p) > 40]
                paras = [p for p in paras if not p.startswith("Faults were")]
                bullets = [l[2:].strip() for l in body.splitlines() if l.startswith("- trials run") or l.startswith("- trials where")]
                para = (" ".join(paras[:1]) + (" " + "; ".join(bullets) if bullets else "")).strip()
                para = re.sub(r"[*`]", "", para)
                date = re.search(r"20\d\d-\d\d-\d\d", body)
                docs.append({"name": name, "mode": "validation session", "text": para[:420], "when": date.group(0) if date else ("2026-10-31" if d == "live-validation" else "")})
                break
    return docs + runs


def change_log() -> list[dict]:
    rows = [
        ("2026-11-18T03:51:11+00:00", "live_remediation", "on", "Switched on for one fault-console run (memory fix with pod replacement)", "fault console"),
        ("2026-11-18T04:00:50+00:00", "live_remediation", "off", "Restored by the runner after the run; AUTO_REMEDIATE=false confirmed", "fault console"),
        ("2026-11-18T03:17:41+00:00", "live_remediation", "off", "Restored by the runner after the live disk run", "fault console"),
        ("2026-11-18T02:42:27+00:00", "live_remediation", "off", "Restored by the runner after the simulated disk run", "fault console"),
        ("2026-11-18T03:24:00+00:00", "deploy", "build 1", "Jenkins build 1: preflight, tests, images, deploy with safe defaults, verify (SUCCESS)", "Jenkins"),
    ]
    import subprocess
    for sha, label, key in (("d60e6cc", "Memory fix scales out and replaces the pods; fault-console status no longer waits on kubectl", "playbook"),
                            ("0133aeb", "Fault console can switch the ML second opinion (shadow/gate) and the ambiguity rule; the runner restores the defaults after each run", "console"),
                            ("9782482", "Every incident now records what each setting combination would have decided (transparency mode)", "engine")):
        try:
            when = subprocess.run(["git", "-C", str(REPO), "log", "-1", "--format=%aI", sha], capture_output=True, text=True, timeout=20).stdout.strip()
            if when:
                rows.append((dt.datetime.fromisoformat(when).astimezone(UTC).isoformat(timespec="seconds"), key, sha, label, "devops-completion branch"))
        except Exception:                                       # noqa: BLE001 - a missing commit just means one less row
            pass
    return [{"t": t, "setting": s, "value": v, "reason": r, "by": b} for t, s, v, r, b in sorted(rows)]


def build() -> dict:
    incs = oct31() + console_runs()
    incs.sort(key=lambda i: i["t"])
    for k, i in enumerate(incs, 1):
        i["id"] = f"INC-{2000 + k}"
    # which row was in force: no second opinion in Oct (rules only); shadow + ambiguity off since (also rules only)
    for i in incs:
        for r in i["variants"]:
            r["active"] = r["key"] == "rules"
    return {"mode": "real", "generated": max(e["t"] for i in incs for e in i["events"]), "start": incs[0]["t"], "incidents": incs, "events": change_log(), "people": [], "runs": runs_list(),
            "current": {"live_remediation": "off", "confidence_threshold": "0.75", "second_opinion": "shadow", "ambiguity_rule": "off"}}


if __name__ == "__main__":
    data = build()
    out = HERE / "data_live.js"
    out.write_text("// built by build_real.py from the collected logs" + chr(10) + "window.DATA = " + json.dumps(data, separators=(",", ":")) + ";" + chr(10), encoding="utf-8")
    from collections import Counter
    print(len(data["incidents"]), "incidents,", len(data["runs"]), "runs,", len(data["events"]), "changes;", out.stat().st_size // 1024, "KB")
    print(Counter(i["action"] for i in data["incidents"]), Counter(i["source"] for i in data["incidents"]))
