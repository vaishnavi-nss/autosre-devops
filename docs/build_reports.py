"""Rebuild the two short PDF reports in this folder.

    python docs/build_reports.py

Needs `reportlab`. The numbers are typed in below from the run summaries (`live-validation-run*/summary.md`) and
`evaluation.md`; when a new run changes them, edit them here and rerun, so the PDFs do not go stale again.
"""
from __future__ import annotations

from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

OUT = Path(__file__).resolve().parent
S = getSampleStyleSheet()
BODY = ParagraphStyle("body", parent=S["BodyText"], fontSize=8.6, leading=11, spaceAfter=2)
H1 = ParagraphStyle("h1", parent=S["Heading1"], fontSize=15, leading=18, spaceAfter=3)
H2 = ParagraphStyle("h2", parent=S["Heading2"], fontSize=10.5, leading=13, spaceBefore=6, spaceAfter=2, keepWithNext=1)
SUB = ParagraphStyle("sub", parent=BODY, textColor=colors.HexColor("#444444"))
BUL = ParagraphStyle("bul", parent=BODY, leftIndent=10, bulletIndent=0)
CELL = ParagraphStyle("cell", parent=BODY, fontSize=8, leading=9.6, spaceAfter=0)


def bullets(items: list[str]) -> list:
    return [Paragraph(t, BUL, bulletText="•") for t in items]


def table(rows: list[list[str]], widths: list[float]) -> Table:
    data = [[Paragraph(c, CELL) for c in r] for r in rows]
    t = Table(data, colWidths=[w * mm for w in widths])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e6ebf2")),
        ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#999999")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 1.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5),
    ]))
    return t


def build(name: str, title: str, author: str, story: list) -> None:
    doc = SimpleDocTemplate(str(OUT / name), pagesize=A4, leftMargin=16 * mm, rightMargin=16 * mm,
                            topMargin=13 * mm, bottomMargin=12 * mm, title=title, author=author)
    doc.build(story)


GATE = [
    ["Gate bar (written before the data)", "Retrained model, runs 11-14"],
    ["4 sessions with the fix (need 3)", "met"],
    ["Judged correct decisions (need 40), first decisions (need 20)", "46, 26"],
    ["Vetoes of correct decisions; 95% upper bound (need 10% or less)", "0 of 46; 7.7%"],
    ["True claims backed (need 95% of 40 probes or more)", "50 of 52"],
    ["Wrong claims caught (need 95% of 40 probes or more)", "59 of 62"],
    ["Unknown-cause trials escalated (need 5)", "5 of 5 (all from run 11)"],
]

ML = [
    Paragraph("AutoSRE: ML implementation report", H1),
    Paragraph("Root-cause analysis for a banking chain: Frontend, API Gateway, Transaction Service, Database. Code: "
              "github.com/vaishnavi-nss/autosre-devops (branches ml-cloud-additions and ml-and-cloud-validation). "
              "Status as of 13 Nov 2026, after live run 15.", SUB),
    Paragraph("1. What the ML part is", H2),
    *bullets([
        "<b>Rule-based correlation engine (the decision maker).</b> Alertmanager groups Prometheus alerts and posts "
        "them to the engine. It picks the deepest alerting service in the dependency chain, a signature table gives "
        "the root cause, and a weighted confidence is attached. Unknown patterns escalate to a human, never a guess.",
        "<b>Learned scorer (a second opinion, not the decision maker).</b> A separate read-only service pulls raw "
        "metrics from Prometheus, scores each service, and says agree, disagree or no opinion on the engine's "
        "diagnosis. It runs in <b>shadow</b> mode: it is logged, it never blocks a fix.",
    ]),
    Paragraph("2. How the scorer works", H2),
    *bullets([
        "Features: per service, a robust z-score of how far each metric (cpu, memory, latency, errors, workload) "
        "moved from its own earlier behaviour. A random forest ranks the culprit service; a calibrated gradient-boosting "
        "classifier names the fault; confidence is P(service) x P(fault).",
        "Two training sources. <b>Public benchmark:</b> RCAEval RE1 (375 cases) and RE2 (270 cases), tested "
        "leave-one-system-out. <b>Our own cluster:</b> windows recorded during live fault injection. The scorer in use now "
        "(model b565d77f) is trained on runs 3, 4, 5-early and 8: 53 trials, 127 windows. Holding out one trial at a "
        "time it names the right service 95.3% and the right service and fault 93.7% of the time, backs 100 of 104 true "
        "claims and endorses 2 of 127 wrong ones.",
        "Fault start: the engine sends the time the deciding alert's fault began (its start minus its own <i>for</i> "
        "delay), not the earliest start of every alert firing. Without this a stale alert made the scorer veto correct "
        "diagnoses (found in run 7). In run 9, with a stale alert deliberately left firing, the old start disagreed "
        "with the true cause in 10 of 12 probes and the new start in 0 of 12.",
    ]),
    Paragraph("3. Public benchmark (RCAEval)", H2),
    table([["Suite", "Replay baseline (rule-style)", "Learned model"],
           ["RE1 (375 cases)", "28% wrong, 37% right", "61% right when forced to act; 16% wrong when gated at 0.5"],
           ["RE2 (270 cases)", "48% wrong", "63.7% right; precision 69% at gate 0.5 up to 82% at 0.9"]],
          [32, 55, 95]),
    Paragraph("Remaining errors are inherent confusions: cpu vs disk, delay vs loss, memory read as disk. Adding logs "
              "gave no gain. The benchmark-trained scorer did <b>not</b> transfer to our system (it disagreed with 8 of "
              "13 correct rule decisions in the first live test), which is why the scorer was retrained on our own data.",
              BODY),
    Paragraph("4. Live results on our AWS cluster", H2),
    Paragraph("The rule engine made 0 wrong actions in the 32 injected-fault trials of the first two runs. The scorer's "
              "gate bar was written down before the data (<i>docs/evaluation.md</i>, checked by "
              "<i>evaluation/gate_check.py</i>). On the retrained model alone, runs 11 to 14:", BODY),
    table(GATE, [125, 57]),
    Paragraph("<b>Every probe miss was a no_opinion</b> (typically at the first incident, before a window exists); the "
              "scorer never endorsed a wrong claim and never disagreed with a true one. <b>The bar is met, "
              "SECOND_OPINION_MODE stays shadow</b>; switching it on is a separate decision, best one cause at a time, "
              "treating no_opinion as pass-through.", BODY),
    Paragraph("5. Why live trials were lost, and the fix", H2),
    Paragraph("About a third of live trials were discarded as contaminated: APILatencyHigh would not clear. Cause "
              "(run 14 and 15): the services' 200m CPU limits throttle them (22% of CPU periods), which puts the idle "
              "gateway p95 at 0.45-0.77 s, on the alert's 0.5 s line. With the limit at 1000m the idle p95 fell to about "
              "0.2 s and a 3-trial run had no contaminated trial. The live_validate.py flag <i>--cpu-limit</i> applies it. "
              "The gate evidence was gathered under the 200m limits; three trials at 1000m agree with it but are not enough "
              "to pool.", BODY),
    Paragraph("6. Still open", H2),
    *bullets([
        "Decide whether to turn the gate on (shadow today), and ESCALATE_ON_AMBIGUITY (off). Both were run on and off live "
        "with two real faults (runs 16, 17): the rule costs nothing on 130 real single-fault webhooks and escalates every "
        "two-fault incident (0 of 6 trials acted, against 9 of 9 with it off); the gate alone does not stop action on the "
        "dominant fault (the scorer misses the second fault), so it is a backstop. No wrong action in either.",
        "A session at the higher CPU limit, if the result is to be relied on for a differently sized cluster. "
        "Why contamination worsened late in a long session (run 13) is not shown.",
        "Unknown-cause evidence is from run 11 only. Faults are synthetic, on a small cluster.",
    ]),
    Paragraph("7. Where to find it", H2),
    *bullets([
        "correlation-engine/: rules, engine, evaluation scripts (rcaeval_eval.py, live_validate.py, gate_check.py, "
        "diagnose_latency.py). second-opinion/: the scorer service, train.py (benchmark), train_own.py (our data).",
        "docs/evaluation.md (full numbers and the gate bar), docs/live-validation-run*/summary.md (one per session), "
        "docs/aws-validation.md (repeating the run).",
    ]),
]

CLOUD = [
    Paragraph("AutoSRE: cloud handoff report", H1),
    Paragraph("Live validation on AWS and what the cloud role needs to do. Repo: vaishnavi-nss/autosre-devops. "
              "Status as of 13 Nov 2026.", SUB),
    Paragraph("Where the code is", H2),
    *bullets([
        "<b>ml-cloud-additions</b>: the PR branch. Adds files only; nothing on main is modified.",
        "<b>ml-and-cloud-validation</b>: full history, including files that overlap with main.",
        "The pull request is not opened; its base branch must be main.",
    ]),
    Paragraph("Live validation", H2),
    Paragraph("AWS Academy lab, one t3.large (unlimited CPU credits) with minikube and kube-prometheus-stack. Faults were "
              "injected through the CHAOS_ENABLED endpoints; the alert rules, Alertmanager, webhook and engine were the real "
              "pipeline. Sessions run 1 to 15 (run 10 was void: a deployment mistake, no data).", BODY),
    table([["Measure", "Result"],
           ["Rule engine, runs 1-2", "32 injected-fault trials, 0 wrong actions"],
           ["Time for an incident to reach the engine", "about 2 minutes after injection"],
           ["Alerts clear after a fault ends", "about 5 minutes (rate()[5m] windows)"],
           ["Second-opinion scorer, runs 11-14 (retrained model)", "46 judged decisions, 0 vetoes, upper bound 7.7%; "
            "gate bar met; every miss a no_opinion"],
           ["Trials lost to contamination, runs 11-14", "15 of 46"]],
          [70, 112]),
    Paragraph("The finding that matters for the cloud role", H2),
    Paragraph("<b>The services' 200m CPU limits are too low for the traffic.</b> They throttle about 22% of CPU periods "
              "while the node is only about 45% busy, which puts the idle gateway p95 (0.45-0.77 s) on the 0.5 s line of "
              "APILatencyHigh. So that alert can fire with no fault present and then will not clear, and every service is "
              "slower than it needs to be. Measured on the live cluster: at 1000m the idle p95 was about 0.2 s "
              "(transaction-service 0.07 s, was 0.25-0.44 s) and the alert stayed quiet.", BODY),
    *bullets([
        "Raise the CPU limits in k8s/api-gateway.yaml, transaction-service.yaml, database.yaml and frontend.yaml. "
        "These files exist on main, so the PR does not touch them. The memory limit (128Mi) is what the memory fault "
        "depends on; leave it.",
        "If the limits must stay low, raise the 0.5 s threshold in monitoring/alert-rules.yaml, or lengthen its for: "
        "clause; the healthy range is inside it.",
        "Until then, the harness takes <i>--cpu-limit 1000m</i> and logs it.",
    ]),
    Paragraph("What to do by hand", H2),
    Paragraph("These files already exist on main, so the PR leaves them out: scripts/build-images.sh (also build "
              "autosre/second-opinion:dev), scripts/deploy.sh (wait for the second-opinion rollout), "
              "k8s/correlation-engine.yaml (set SECOND_OPINION_URL and SECOND_OPINION_MODE=shadow). "
              "k8s/second-opinion.yaml is new and included. Set the dispatcher cooldown to at least 10 minutes: alerts "
              "keep firing about 5 minutes after a fault ends.", BODY),
    Paragraph("Running a session", H2),
    Paragraph("Press Start Lab, wait for green, open the console with the lab's own AWS link (a restart signs the console "
              "out), then in CloudShell: <i>curl -fsS https://raw.githubusercontent.com/vaishnavi-nss/autosre-devops/"
              "ml-and-cloud-validation/scripts/aws/start-live-run.sh | RUN=run16 PER_CAUSE=3 UNKNOWN=0 DEADLINE=120 bash</i>. "
              "It rebuilds the images, replaces the pods, runs the harness and syncs results to the bucket every 45 s. "
              "Allow about 12 minutes to prepare and 6-14 minutes per trial; the lab gives 4 hours. The harness stops "
              "starting trials at the deadline.", BODY),
    Paragraph("AWS Academy limits", H2),
    *bullets([
        "Instance types above large are denied. No IAM role creation: use LabInstanceProfile. Session about 4 hours.",
        "CloudShell drops idle sessions; reconnect and retype. The S3 console serves .jsonl files as downloads.",
    ]),
    Paragraph("Still open", H2),
    *bullets([
        "Bucket autosre-validation-841736140404 (raw results of the sessions), the instance i-04f8f4d75378a1aba and an unused "
        "security group are kept on purpose; nothing was cleaned up. The VLE*-jenkins instances in the lab account may "
        "still be running; they were not touched.",
        "Recommendation unchanged: keep SECOND_OPINION_MODE=shadow. The scorer now meets the written gate bar on our "
        "own data, so enabling it is a decision for the team, one cause at a time.",
    ]),
]

if __name__ == "__main__":
    build("ml-implementation-report.pdf", "AutoSRE: ML implementation report", "AutoSRE ML / validation", ML)
    build("cloud-handoff-report.pdf", "AutoSRE cloud handoff report", "AutoSRE ML / validation", CLOUD)
    print("wrote 2 PDFs in", OUT)
