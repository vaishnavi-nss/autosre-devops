"""Builds docs/completion/AutoSRE-ML-code-and-results.pdf (10 pages) from the real code and the real result files.

    python docs/completion/ml-report/make_ml_report.py

Numbers come from second-opinion/models/*.json where they exist; the simulated-incident figures come from docs/evaluation.md and
the RCAEval ones from the model json. Nothing here is invented: where a figure is an illustration it says so.
"""
from __future__ import annotations

import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.patches import FancyBboxPatch
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (Image, KeepTogether, PageBreak, Paragraph, Preformatted, SimpleDocTemplate, Spacer, Table, TableStyle)

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / "docs" / "completion" / "AutoSRE-ML-code-and-results.pdf"
FIG = ROOT / "docs" / "completion" / "ml-report" / "figures"
FIG.mkdir(parents=True, exist_ok=True)
M = ROOT / "second-opinion" / "models"

NAVY, GOLD, INK, MUTE, OK, BAD, LINE = "#141c33", "#b08d3c", "#1b2236", "#6b7280", "#1f7a4d", "#b3261e", "#e2dfd5"
BLUE, ORANGE, GREY = "#3b6fd4", "#e08a2b", "#9aa3b8"

own = json.loads((M / "own.json").read_text())
real = json.loads((M / "own_real.json").read_text())
own2 = json.loads((M / "own_v2.json").read_text())           # the model trained on the older trials plus the Kubernetes-state fault trials (the default now)
L2 = own2["leave_one_trial_out"]
J2 = own2["judge_on_held_out_windows"]
rca = json.loads((M / "second_opinion.json").read_text())
LOTO = own["leave_one_trial_out"]
RC4 = rca["leave_one_system_out_four_services"]
RC = rca["leave_one_system_out"]


# ----------------------------------------------------------------------------------------------------------- figures
def box(ax, x, y, w, h, text, fc, ec=NAVY, fs=9, tc="white"):
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.02,rounding_size=0.08", fc=fc, ec=ec, lw=1.2))
    ax.text(x + w / 2, y + h / 2, text, ha="center", va="center", fontsize=fs, color=tc, wrap=True)


def fig_pipeline():
    fig, ax = plt.subplots(figsize=(9.2, 3.1))
    ax.set_xlim(0, 12); ax.set_ylim(0, 4); ax.axis("off")
    steps = [("Real fault\nin the autosre\nnamespace", "#6b7280"), ("Prometheus\nalert rules", NAVY), ("Alertmanager\n30 s group", NAVY),
             ("Correlation\nengine: RULES\nname the cause", BLUE), ("ML second\nopinion: raw\nmetrics say\nagree or not", ORANGE),
             ("Dispatcher\ngate: conf\n>= 0.75 and\nML agrees", "#7a4ab0"), ("Ansible\nplaybook, then\nKubernetes\nrecovery", OK)]
    xs = np.linspace(0.1, 10.4, len(steps))
    for (t, c), x in zip(steps, xs):
        box(ax, x, 1.5, 1.45, 1.8, t, c, fs=7.6)
    for a, b in zip(xs[:-1], xs[1:]):
        ax.annotate("", xy=(b, 2.4), xytext=(a + 1.45, 2.4), arrowprops=dict(arrowstyle="->", color=INK, lw=1.3))
    ax.text(6, 0.95, "ML never starts anything. It can only say 'agree', 'disagree' or 'no opinion' about a diagnosis the rules already made.",
            ha="center", fontsize=8.6, color=INK, style="italic")
    ax.text(6, 0.4, "If the ML disagrees, has no opinion, or is down: the dispatcher escalates to a human and runs nothing.", ha="center", fontsize=8.6, color=BAD)
    fig.tight_layout(); p = FIG / "f1_pipeline.png"; fig.savefig(p, dpi=170); plt.close(fig); return p


def fig_sim():
    fig, ax = plt.subplots(figsize=(6.4, 2.6))
    labels = ["Correct action", "Missed (escalated a known cause)", "WRONG action"]
    mod, hard = [64.3, 8.7, 1.5], [51.5, 17.6, 5.5]
    x = np.arange(3); w = 0.36
    b1 = ax.bar(x - w / 2, mod, w, color=BLUE, label="moderate noise (accuracy 89.9%)")
    b2 = ax.bar(x + w / 2, hard, w, color=ORANGE, label="hard noise (accuracy 76.9%)")
    for b in list(b1) + list(b2):
        ax.text(b.get_x() + b.get_width() / 2, b.get_height() + 1, f"{b.get_height():.1f}%", ha="center", fontsize=8)
    ax.set_xticks(x); ax.set_xticklabels(labels, fontsize=8); ax.set_ylim(0, 78); ax.set_ylabel("% of simulated incidents", fontsize=8)
    ax.legend(fontsize=7.5, frameon=False); ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout(); p = FIG / "f2_rules_sim.png"; fig.savefig(p, dpi=170); plt.close(fig); return p


def fig_zscore():
    rng = np.random.default_rng(3)
    t = np.arange(0, 600, 15)
    base = 40 + rng.normal(0, 2.2, size=len(t))
    y = base.copy(); fault_start = 360
    y[t >= fault_start] += 28 + rng.normal(0, 3, size=(t >= fault_start).sum())
    normal, faulty = y[t < fault_start], y[t >= fault_start]
    med, mad = np.median(normal), 1.4826 * np.median(np.abs(normal - np.median(normal)))
    z = (np.median(faulty) - med) / max(mad, 0.05 * abs(med), 1e-6)
    fig, ax = plt.subplots(figsize=(6.4, 2.5))
    ax.plot(t, y, color=BLUE, lw=1.6)
    ax.axvline(fault_start, color=BAD, ls="--", lw=1.2); ax.text(fault_start + 6, y.max() - 4, "fault starts", color=BAD, fontsize=8)
    ax.axhline(med, color=GREY, lw=1); ax.text(5, med + 1.2, f"baseline median {med:.1f}", fontsize=7.5, color=MUTE)
    ax.axhline(np.median(faulty), color=ORANGE, lw=1); ax.text(5, np.median(faulty) + 1.2, f"fault-window median {np.median(faulty):.1f}", fontsize=7.5, color=ORANGE)
    ax.set_xlabel("seconds (ILLUSTRATION with made-up data, not a cluster measurement)", fontsize=7.5); ax.set_ylabel("one metric", fontsize=8)
    ax.set_title(f"robust z = (fault median - baseline median) / spread = {z:.1f}", fontsize=9)
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout(); p = FIG / "f3_zscore.png"; fig.savefig(p, dpi=170); plt.close(fig); return p


def fig_rcaeval():
    fig, axes = plt.subplots(1, 2, figsize=(9.0, 2.7), gridspec_kw={"width_ratios": [1.3, 1]})
    ax = axes[0]
    names = list(RC["by_fault"].keys()); vals = [RC["by_fault"][k] * 100 for k in names]
    bars = ax.bar(names, vals, color=[BAD if v < 50 else OK if v > 90 else BLUE for v in vals])
    for b, v in zip(bars, vals):
        ax.text(b.get_x() + b.get_width() / 2, v + 1.5, f"{v:.0f}%", ha="center", fontsize=8)
    ax.set_ylim(0, 112); ax.set_title("RCAEval: both service AND fault right, by fault type", fontsize=8.6)
    ax.spines[["top", "right"]].set_visible(False); ax.tick_params(labelsize=8)
    ax = axes[1]
    bysys = RC["by_system"]
    ks = ["service top-1", "fault accuracy", "both right"]
    v1 = [RC["service_top1"] * 100, RC["fault_accuracy"] * 100, RC["both_right"] * 100]
    v2 = [RC4["service_top1"] * 100, RC4["fault_accuracy"] * 100, RC4["both_right"] * 100]
    x = np.arange(3); w = 0.36
    ax.bar(x - w / 2, v1, w, color=GREY, label="all services"); ax.bar(x + w / 2, v2, w, color=BLUE, label="4 services (like ours)")
    for xi, a, b in zip(x, v1, v2):
        ax.text(xi - w / 2, a + 1.5, f"{a:.0f}", ha="center", fontsize=7.5); ax.text(xi + w / 2, b + 1.5, f"{b:.0f}", ha="center", fontsize=7.5)
    ax.set_xticks(x); ax.set_xticklabels(ks, fontsize=7.5); ax.set_ylim(0, 112); ax.legend(fontsize=7, frameon=False)
    ax.set_title("pooled, leave one system out (%)", fontsize=8.6); ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout(); p = FIG / "f4_rcaeval.png"; fig.savefig(p, dpi=170); plt.close(fig); return p


def fig_own():
    v = LOTO["verdicts"]
    fig, ax = plt.subplots(figsize=(6.6, 2.3))
    cats = ["TRUE claim\n(the cause really injected)", "WRONG claim\n(some other cause)"]
    agree = [v["true_claim"]["agree"], v["wrong_claim"]["agree"]]
    dis = [v["true_claim"]["disagree"], v["wrong_claim"]["disagree"]]
    noop = [v["true_claim"]["no_opinion"], v["wrong_claim"]["no_opinion"]]
    y = np.arange(2)
    ax.barh(y, agree, color=OK, label="agree"); ax.barh(y, dis, left=agree, color=BAD, label="disagree"); ax.barh(y, noop, left=np.add(agree, dis), color=GREY, label="no opinion")
    for yi, a, d in zip(y, agree, dis):
        ax.text(a / 2, yi, str(a), ha="center", va="center", color="white", fontsize=9) if a > 8 else None
        ax.text(a + d / 2, yi, str(d), ha="center", va="center", color="white", fontsize=9) if d > 8 else None
        if a <= 8: ax.text(a + 1, yi, f"{a}", va="center", fontsize=8, color=OK)
        if d <= 8: ax.text(a + d + 1, yi, f"{d}", va="center", fontsize=8, color=BAD)
    ax.set_yticks(y); ax.set_yticklabels(cats, fontsize=8); ax.invert_yaxis(); ax.set_xlabel("held-out windows (127, leave one trial out)", fontsize=8)
    ax.legend(fontsize=7.5, frameon=False, ncol=3, loc="lower center", bbox_to_anchor=(0.5, 1.0)); ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout(); p = FIG / "f5_own_verdicts.png"; fig.savefig(p, dpi=170); plt.close(fig); return p


# ----------------------------------------------------------------------------------------------------------- document
ss = getSampleStyleSheet()
H1 = ParagraphStyle("H1", parent=ss["Heading1"], fontName="Helvetica-Bold", fontSize=17, textColor=colors.HexColor(NAVY), spaceAfter=4, leading=20)
H2 = ParagraphStyle("H2", parent=ss["Heading2"], fontName="Helvetica-Bold", fontSize=11.5, textColor=colors.HexColor(NAVY), spaceBefore=8, spaceAfter=3)
B = ParagraphStyle("B", parent=ss["BodyText"], fontName="Helvetica", fontSize=9.4, leading=13.2, textColor=colors.HexColor(INK), alignment=TA_LEFT, spaceAfter=4)
SM = ParagraphStyle("SM", parent=B, fontSize=8.2, leading=11, textColor=colors.HexColor(MUTE))
CELL = ParagraphStyle("CELL", parent=B, fontSize=8.4, leading=11, spaceAfter=0)
CELLB = ParagraphStyle("CELLB", parent=CELL, fontName="Helvetica-Bold")
CODE = ParagraphStyle("CODE", fontName="Courier", fontSize=6.7, leading=8.6, textColor=colors.HexColor("#d7dcec"))
CALL = ParagraphStyle("CALL", parent=B, backColor=colors.HexColor("#f4f2ec"), borderPadding=(6, 8, 6, 8), spaceBefore=4, spaceAfter=8, borderColor=colors.HexColor(GOLD), borderWidth=0.8)
W = A4[0] - 30 * mm


def P(t, s=B):
    return Paragraph(t, s)


def code(t):
    text = t.strip("\n")
    lines = text.splitlines()
    longest = max(len(line) for line in lines)
    assert longest <= 122, f"code line too long ({longest}): {max(lines, key=len)[:70]}"
    cell = Table([[Preformatted(text, CODE)]], colWidths=[W])
    cell.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), colors.HexColor(NAVY)), ("LEFTPADDING", (0, 0), (-1, -1), 7),
                              ("RIGHTPADDING", (0, 0), (-1, -1), 5), ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
    return KeepTogether([Spacer(1, 3), cell, Spacer(1, 6)])


def table(rows, widths, head=True, zebra=True):
    data = [[Paragraph(str(c), CELLB if (head and i == 0) else CELL) for c in r] for i, r in enumerate(rows)]
    t = Table(data, colWidths=widths, repeatRows=1 if head else 0)
    st = [("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor(LINE)), ("VALIGN", (0, 0), (-1, -1), "TOP"),
          ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4), ("TOPPADDING", (0, 0), (-1, -1), 2.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5)]
    if head:
        st.append(("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#ece8dc")))
    if zebra:
        for i in range(1, len(rows)):
            if i % 2 == 0:
                st.append(("BACKGROUND", (0, i), (-1, i), colors.HexColor("#faf9f5")))
    t.setStyle(TableStyle(st))
    return t


def img(path, width=W, ratio=None):
    from PIL import Image as PI
    w, h = PI.open(path).size
    return Image(str(path), width=width, height=width * h / w)


def footer(canvas, doc):
    canvas.saveState()
    canvas.setFont("Helvetica", 7.5); canvas.setFillColor(colors.HexColor(MUTE))
    canvas.drawString(15 * mm, 9 * mm, "AutoSRE: the ML code and its results (branch devops-completion)")
    canvas.drawRightString(A4[0] - 15 * mm, 9 * mm, f"page {doc.page} of 10")
    canvas.restoreState()


def build():
    f1, f2, f3, f4, f5 = fig_pipeline(), fig_sim(), fig_zscore(), fig_rcaeval(), fig_own()
    s = []

    # ---------------- 1
    s += [P("AutoSRE: the ML code and what it achieved", H1),
          P("A guide for teammates who need to understand the machine-learning part without reading all of it. Ten pages: where the ML sits, how it turns "
            "metrics into a verdict, how it was trained and tested, what the numbers really say, and what they do not.", SM), Spacer(1, 4),
          P("The one-paragraph version", H2),
          P("AutoSRE fixes cluster faults on its own, but only when two independent things agree. First, a <b>hand-written rule engine</b> reads Prometheus alerts and names a "
            "root cause with a confidence. Second, a small <b>machine-learning model (the 'second opinion')</b> looks at the raw metrics, which the rules never see, and says "
            "whether it reaches the same conclusion. The dispatcher runs a fix only if the rules are confident (>= 0.75) <i>and</i> the ML agrees (for the Kubernetes-state faults the model was not trained on, it says 'no opinion', which the dispatcher accepts; a real 'disagree' always stops it). The ML cannot start a fix by itself; "
            "it is a safety check. Everything below is about that check: its code, its training data, and how well it works."),
          P("The numbers to remember", H2)]
    s.append(table([
        ["What was measured", "Result", "What it means"],
        ["Rule engine, simulated incidents", f"89.9% correct (moderate noise), 76.9% (hard); wrong actions 1.5% and 5.5%", "Simulated data written by the same people as the rules: a sanity check, not a real-world rate."],
        ["ML model trained on a public dataset (RCAEval RE1)", f"finds the right service {RC['service_top1']*100:.0f}% of the time, service AND fault {RC['both_right']*100:.0f}%", "Did not transfer well to our cluster, so we trained on our own faults."],
        ["ML model trained on our cluster (own_v2.joblib, the default), held-out trials", f"service {L2['service']*100:.1f}%, service and fault {L2['service_and_fault']*100:.1f}% ({L2['windows']} windows from 85 trials; the first version, own.joblib: {LOTO['service_accuracy']*100:.1f}% and {LOTO['service_and_fault_accuracy']*100:.1f}%)", "Each trial is scored by a model that never saw it."],
        ["Same, as a safety check", f"backs the TRUE claim {J2['true_claims']['agree']} of {sum(J2['true_claims'].values())} times; backs a WRONG claim only {J2['wrong_claims']['agree']} of {sum(J2['wrong_claims'].values())} (kept fault kinds)", "It is hard to fool and rarely cries wolf."],
        ["Real faults on the cluster (not used for training)", "40 of 40 and 32 of 32 windows right; live verdicts agree 10 of 10 and 8 of 8", "Real leaked slots, real held memory, real disk fill."],
        ["Retraining on those real faults", "no measurable gain (40/40 and 32/32 either way)", "So own_real.joblib stays opt-in."],
        ["Retraining on Kubernetes-state faults (final sessions)", "32 new trials (two of each of 16 causes); only fault kinds at 95% or better held out are kept", "own_v2.joblib is the default now; own.joblib is the previous model."]],
        [W * 0.27, W * 0.40, W * 0.33]))
    s += [Spacer(1, 6), P("<b>How to read the rest.</b> Pages 2 to 5 explain the design and the code. Pages 6 to 8 are training and results. Page 9 shows how the verdict is used live "
                          "and the one case where the ML disagreed. Page 10 lists the limits honestly and how to reproduce everything.", B), PageBreak()]

    # ---------------- 2
    s += [P("1. Where the ML sits in the pipeline", H1), img(f1, W), Spacer(1, 4),
          P("Three layers, each with one job. The <b>rule engine</b> is deterministic: it maps each alert to a service in the dependency chain (frontend, api-gateway, transaction-service, database), "
            "takes the deepest alerting service as the candidate, matches a symptom signature, and reports a weighted confidence. There is no learning in it, which is deliberate: it prints an auditable sentence "
            "('transaction-service is the deepest alerting service; its deciding symptom DBPoolNearExhaustion fired; 4 of 4 expected symptoms present; confidence 100%')."),
          P("The <b>ML second opinion</b> (the <font face='Courier'>second-opinion</font> service) asks a different question: using only the raw time series (CPU, memory, latency, errors, request rate, disk, "
            "connection pool and seven Kubernetes-state signals such as replicas available or paused), which service looks most broken, and what kind of fault? If its answer matches the rules' cause, it answers <i>agree</i>."),
          P("The <b>dispatcher</b> applies the gate. These are the settings that decide whether anything runs:"),
          table([["Setting", "Default", "Meaning"],
                 ["AUTO_REMEDIATE", "false", "Nothing executes at all unless this is switched on for a named live run."],
                 ["MIN_CONFIDENCE", "0.75", "The rule engine's confidence must be at least this."],
                 ["SECOND_OPINION_POLICY", "agree", "The ML must answer 'agree'. 'disagree' and 'unavailable' always escalate to a human, and so does 'no_opinion' for the original three causes. For the newer Kubernetes-state causes 'no_opinion' is accepted, because the scorer cannot see some of those faults."],
                 ["COOLDOWN_SECONDS", "600", "The same fix for the same cause and service is not repeated inside this window."]],
                [W * 0.27, W * 0.13, W * 0.60]),
          Spacer(1, 6),
          P("Why two opinions? <b>The rules' confidence measures how complete the symptom set is, not whether the diagnosis is right.</b> In simulation, wrong calls averaged 0.84 confidence against 0.93 for right ones, "
            "so a confidence gate alone catches few mistakes. The ML looks at independent evidence (metrics, not alerts), which is what makes a disagreement informative.", CALL),
          P("Code map: <font face='Courier'>correlation-engine/correlate.py</font> (rules), <font face='Courier'>second-opinion/scoring.py</font> (features and model), "
            "<font face='Courier'>second-opinion/opinion.py</font> (the agree/disagree judge), <font face='Courier'>correlation-engine/second_opinion.py</font> (the client the engine uses), "
            "<font face='Courier'>remediation-dispatcher/app.py</font> (the gate).", SM), PageBreak()]

    # ---------------- 3
    s += [P("2. The rule engine: the part that is not ML", H1),
          P("It is worth understanding first, because the ML is judged against it. Each root cause has a signature with weighted symptoms. Confidence is the share of the total weight that actually fired:"),
          code("""
confidence = sum(weights of symptoms that fired) / sum(all weights)

db_connection_pool_exhaustion  DBPoolNearExhaustion 0.55 | ErrorRateHigh 0.20 | PodRestartsHigh 0.15 | APILatencyHigh 0.10
high_memory_usage              ContainerMemoryHigh   0.60 | PodRestartsHigh 0.25 | APILatencyHigh 0.15
db_disk_full                   DBDiskNearFull        0.60 | ErrorRateHigh 0.25  | APILatencyHigh 0.15
# ... and fourteen Kubernetes-state causes (service down, bad rollout, selector, paused, Service type, extra replicas),
# each with its own deciding alert; see correlation-engine/signatures.py. Seventeen causes in all.
"""),
          P("Two properties matter. First, a cause cannot be named unless its <b>deciding symptom</b> (the heavy one) fired on the service that owns it; confidence only ranks matches, it never creates one. "
            "Second, a lone deciding symptom scores 0.55 to 0.60, below the 0.75 gate, so the first alert of every incident is correctly escalated and the engine acts only on a later alert once a "
            "corroborating symptom has fired (earlier live validation: median 83 s to the first incident, 138 s to one the dispatcher acts on). "
            "The exceptions are written down and tested: the drift causes (paused, Service type, extra replicas) and the two API-gateway causes are named on their deciding alert alone, because they are wrong by themselves or cannot have a second symptom."),
          P("<b>It fails safe.</b> If the deepest alerting service has no matching signature, the engine returns <i>None</i>, which means 'escalate to a human'. It does not fall back to a shallower service "
            "that does match, because a deeper service alerting for an unknown reason is the likeliest true cause and restarting the wrong thing is worse than paging someone."),
          P("How it scored on simulated incidents (5,000 per setting). <i>Simulated</i> means the same authors wrote the faults and the rules, so treat it as a behaviour test, not an accuracy claim:", B),
          img(f2, W * 0.78),
          P("Every wrong action in both runs came from one situation: a second cause's deciding symptom was also present (two faults, or a misleading signal). That is exactly the case the ML second opinion and the "
            "optional <font face='Courier'>ESCALATE_ON_AMBIGUITY</font> switch are there for. A decision tree trained on the simulator scored higher on accuracy (94.9%) but made <i>more</i> wrong actions (2.5% against 1.5%), "
            "so accuracy alone would have picked the wrong winner; the rules stayed.", B), PageBreak()]

    # ---------------- 4
    s += [P("3. From raw metrics to features (scoring.py)", H1),
          P("The ML never sees alerts. It sees a window of per-service metrics from Prometheus and asks: how far has each metric moved from its own earlier behaviour? The measure is a <b>robust z-score</b>: "
            "the shift of the median, in units of the metric's normal spread (median absolute deviation), with floors so a perfectly flat metric does not produce an infinite score."),
          code("""
def robust_z(normal, faulty):
    base = normal.median()
    mad = (normal - base).abs().median() * 1.4826           # spread that ignores outliers
    scale = max(mad, normal.std(), 0.05 * abs(base), 1e-6)   # floors: flat metrics must not explode
    z = (faulty.median() - base) / scale
    return clip(z, -10_000, 10_000)
"""),
          img(f3, W * 0.78),
          P("Each metric column is then collapsed to one number per (service, kind). A fault is a <i>rise</i>, so the largest z wins; for request rate a <i>drop</i> matters as much as a rise, so the largest absolute value wins:"),
          code("""
KINDS     = ("cpu", "mem", "latency", "error", "workload")        # RCAEval model: what its data had
OWN_KINDS = KINDS + ("disk", "pool")                              # our cluster also exposes disk and pool
STATE_KINDS = ("down", "noep", "paused", "svctype", "extra", "restart", "unready")   # cluster state
ALL_KINDS = OWN_KINDS + STATE_KINDS                               # what the deployed model reads

row[kind] = max(values, key=abs) if kind == "workload" else max(values)
"""),
          P("The same function builds training features (from recorded windows) and live features (from Prometheus), so the two cannot drift apart. Then each service becomes one feature row: its own scores, "
            "each score as a ratio of the case's maximum, whether it is the maximum, and (for our cluster) which of the four services it is, because 'disk is hot' almost always means the database."),
          code("""
def service_rows(scores, kinds, identity=()):
    top = {k: max(v[k] for v in scores.values()) for k in kinds}
    for service, v in scores.items():
        row  = [v[k] for k in kinds]                                   # raw scores
        row += [v[k] / top[k] if top[k] > 1e-6 else 0 for k in kinds]  # relative to the hottest service
        row += [float(v[k] >= top[k] - 1e-9) for k in kinds]           # is this service the hottest?
        row += [float(service == s) for s in identity]                 # which service is this
"""),
          P("Design choice to remember: nothing in the features depends on <i>how many</i> services a system has. RCAEval systems have 12 to 100+ services and ours has four, so counts or positions would not transfer.", CALL),
          PageBreak()]

    # ---------------- 5
    s += [P("4. The model (the Scorer class)", H1),
          P("The Scorer is two small scikit-learn models chained together, plus a calibration step:"),
          table([["Stage", "Model", "Question it answers", "Output"],
                 ["1. Service ranker", "Gradient boosting (depth 2, 40 rounds) on our data; random forest on RCAEval", "Of the candidate services, which one is the root cause?", "P(service) for each service"],
                 ["2. Fault classifier", "Gradient boosting wrapped in Platt-sigmoid calibration (3-fold)", "Given that service, what kind of fault is it?", "P(fault) for each fault"],
                 ["3. Confidence", "P(service) normalised x P(fault), then Platt scaling fitted on held-out predictions", "How much should this verdict be trusted?", "one number, 0 to 1"]],
                [W * 0.16, W * 0.30, W * 0.32, W * 0.22]), Spacer(1, 4),
          code("""
def diagnose(self, scores, calibrated=True):
    names, rows = service_rows(scores, self.kinds, self.identity)
    p = self.service_model.predict_proba(rows)[:, 1]              # P(this service is the culprit)
    best = int(np.argmax(p))
    p_service = p[best] / max(p.sum(), 1e-9)                      # share among all candidates
    proba = self.fault_model.predict_proba([fault_row(scores, names[best], self.kinds)])[0]
    j = int(np.argmax(proba))
    conf = float(p_service * proba[j])                            # raw confidence
    if calibrated and self.scaler is not None:                    # Platt scaling on held-out data
        conf = float(self.scaler.predict_proba(_logit([conf]).reshape(-1, 1))[0, 1])
    return names[best], str(self.fault_model.classes_[j]), conf, ranking
"""),
          P("<b>Why gradient boosting for our data.</b> Our labelled set is small (about 230 windows). A random forest's bootstrap samples often miss the one positive row for a service, "
            "while boosting separates the few rows better (checked on synthetic data). <b>Why a signed log squash (optional).</b> A z of 10,000 (a flat metric that moved) and a z of 20 are both 'very hot' but differ 500-fold; trees waste their splits on the tail, "
            "so <font face='Courier'>squash_scores</font> applies sign(x)*log1p(|x|)."),
          P("<b>Why calibration matters.</b> Raw model probabilities are over-confident on small data. Calibration is fitted on predictions made by models that did not train on the case being scored "
            "(each system or trial held out in turn), so the final number is closer to 'how often is a verdict this confident actually right'. It is still an estimate from few trials, not a guarantee.", B),
          P("<b>The trap this design avoids.</b> The model is only ever asked to agree or disagree with a diagnosis the rules produced; its output is compared by <font face='Courier'>judge()</font> (page 9). "
            "It cannot create an action, so a wrong ML answer can only cause an unnecessary escalation, never a wrong fix.", CALL),
          PageBreak()]

    # ---------------- 6
    s += [P("5. Training data and how the experiments were run", H1),
          P("Four model files exist in <font face='Courier'>second-opinion/models/</font>. The image ships <b>own_v2.joblib</b> by default; the others are kept for comparison and as fallbacks."),
          table([["File", "Trained on", "Size", "Used for"],
                 ["second_opinion.joblib", "RCAEval RE1 (public, MIT licence): real fault injections on Online Boutique, Sock Shop and Train Ticket", f"{rca['cases']} cases, 3 systems, first {rca['window_s']} s after injection", "The first model. Fault types: cpu, mem, disk, delay, loss. Only memory maps cleanly onto our causes."],
                 ["own_v2.joblib (default)", "The older trials plus 32 trials of Kubernetes-state faults (two of each of 16 causes, four windows each, fault left in place, nothing fixed). Learns the KIND of fault on whichever service; unreliable kinds are left out", f"{own2['trials']} trials, {own2['cases']} windows, labels: {', '.join(own2['faults'])}", "The live second opinion."],
                 ["own.joblib (previous default)", "Our own cluster: faults the harness injected, with the true cause known", f"{own['trials']} trials, {own['cases']} windows, labels: {', '.join(own['faults'])}", "The model that was live until the final sessions."],
                 ["own_real.joblib (opt-in)", "own data plus the REAL faults (leaked pool slots, held memory, real disk fill)", f"{real['old_cases']} old + {real['real_cases']} real windows ({real['real_trials']} real trials)", "The retraining experiment. No measurable gain, so not the default."]],
                [W * 0.2, W * 0.32, W * 0.24, W * 0.24]), Spacer(1, 5),
          P("Where labels come from", H2),
          P("Labels are free: every fault is injected by us, so the true service and fault are known. The scenario runner can record the score table for each probe "
            "(<font face='Courier'>run_scenarios.py --collect</font>, dry run only, because it must keep the fault in place while measuring). The harness asks the scorer service for its feature table at several moments after "
            "each injection; every probe becomes one training case."),
          P("How the test is kept honest", H2),
          code("""
def leave_one_trial_out(cases):
    for system in sorted({c["system"] for c in cases}):             # system = one trial (or one RCAEval system)
        train = [c for c in cases if c["system"] != system]          # all the OTHER trials
        model = Scorer(seed, ALL_KINDS, 60, 40, identity=CHAIN, service="gb")
        model.fit(train).calibrate(train)
        for c in (c for c in cases if c["system"] == system):        # score only the held-out trial
            service, fault, conf, _ = model.diagnose(c["scores"])
"""),
          P("A trial's several probes are held out <b>together</b>. Otherwise a model would be tested on a window whose near-identical twin it trained on, and the score would look far better than it is. "
            "The RCAEval test is stricter still: it holds out an entire <i>system</i> (a different application), which is why those numbers are lower."),
          P("The two ways the model is scored", H2),
          table([["Test", "Question", "Counts as success when"],
                 ["Diagnosis accuracy", "Does the top service (and fault) equal the injected truth?", "service_accuracy, service_and_fault_accuracy"],
                 ["Verdict test (the one that matters)", "Put the TRUE claim and a WRONG claim to judge(), as the live service does.", "agrees with the true claim, does NOT agree with the wrong one"]],
                [W * 0.22, W * 0.46, W * 0.32]),
          P("Sample sizes are small throughout: the model was trained on about 230 windows from 79 trials (255 windows from 85 trials in the held-out test). Treat every percentage here as 'no counter-example found yet', not as a population rate.", CALL),
          PageBreak()]

    # ---------------- 7
    s += [P("6. Result 1: the public-dataset model did not transfer", H1),
          P(f"The first scorer was trained on RCAEval RE1 and tested by <b>leaving one whole system out</b> (373 cases). It finds the right <i>service</i> {RC['service_top1']*100:.0f}% of the time "
            f"({RC4['service_top1']*100:.0f}% when only four services are in play, like ours). It names the <i>fault</i> much less reliably ({RC['fault_accuracy']*100:.0f}%), and the fault type is the hard part:"),
          img(f4, W),
          P("Memory faults are recognised almost perfectly (99%) because memory growth is unmistakable. CPU is the weakest (27%): it overlaps with latency and load. "
            "Packet loss and delay are confused with each other. On our cluster this showed up directly: the model read latency-led faults as packet loss and found the right service only about 62% of the time."),
          P("What the by-system split says", H2),
          table([["Held-out system", "Both right (all services)", "Both right (4 services)"]] +
                [[{"ob": "Online Boutique", "ss": "Sock Shop", "tt": "Train Ticket"}[k], f"{RC['by_system'][k]*100:.1f}%", f"{RC4['by_system'][k]*100:.1f}%"] for k in RC["by_system"]],
                [W * 0.4, W * 0.3, W * 0.3]), Spacer(1, 4),
          P("A model that is right 61% of the time on both questions is useful as a hint but not as a gate. It also has no concept of two of our deciding signals: <b>disk</b> usage and <b>connection-pool</b> usage, because RCAEval has no such columns. "
            "Those are the deciding metrics for two of our three causes."),
          P("Decision: train a model on our own cluster's faults, with disk and pool as extra signals and the service identity as a feature. That is the model on the next page.", CALL),
          P("Confidence gate on this model (RCAEval, all services): acting only when confidence is at least 0.5 keeps 222 of 373 cases at 66% precision; at 0.6 it keeps 130 at 67%; at 0.7 only 12 cases remain. "
            "Raising the gate shrinks coverage faster than it raises precision, which is why the live design pairs a modest confidence floor with an independent agreement check instead of one high threshold.", SM),
          PageBreak()]

    # ---------------- 8
    s += [P("7. Result 2: the model trained on our own cluster", H1),
          P(f"<b>First version (own.joblib).</b> Trained on {own['trials']} injected-fault trials ({own['cases']} windows; pool exhaustion, high memory, disk full and an 'unknown' gateway fault). Leaving one trial out at a time: "
            f"service correct {LOTO['service_accuracy']*100:.1f}% ({round(LOTO['service_accuracy']*127)} of 127), service and fault correct {LOTO['service_and_fault_accuracy']*100:.1f}% ({round(LOTO['service_and_fault_accuracy']*127)} of 127). "
            f"In the verdict test it agreed with the <b>true</b> cause in {LOTO['verdicts']['true_claim']['agree']} of 104 windows and with a <b>wrong</b> cause in only {LOTO['verdicts']['wrong_claim']['agree']} of 127."),
          P("<b>Final version (own_v2.joblib).</b> The first live run of a service-down fault was escalated because this model, which had only seen three causes, said <i>disagree</i> on a fault it could not see. "
            "So 32 trials of the Kubernetes-state faults were collected on the lab (two trials of each of 16 causes), the scorer was given seven state signals, and it was taught the <i>kind</i> of fault on whichever service "
            f"(one class per cause and service had only four examples and scored 23%). Tested leaving one trial out at a time over all 85 trials ({L2['windows']} windows): "
            f"the right service {L2['service']*100:.1f}% of the time, the right service and fault {L2['service_and_fault']*100:.1f}%."),
          table([["Kind of fault", "Windows", "Service and fault right", "Kept"]] +
                [[{"db_connection_pool_exhaustion": "Connection-pool exhaustion", "db_disk_full": "Disk full", "high_memory_usage": "High memory", "service_down": "Service down", "bad_rollout": "Bad rollout",
                   "deployment_paused": "Deployment paused", "service_type_changed": "Service type changed", "service_unreachable": "Service has no endpoint (selector)",
                   "gateway_fault": "An unrelated gateway fault"}.get(k, k), str(v["windows"]), f"{v['accuracy']*100:.0f}%", "background class" if k == "gateway_fault" else ("no (left out)" if k in own2["kinds_dropped"] else "yes")]
                 for k, v in L2["by_kind"].items()],
                [W * 0.40, W * 0.12, W * 0.26, W * 0.22]), Spacer(1, 4),
          P(f"Only kinds at {own2['kinds_kept_bar']*100:.0f}% or better were kept, so for selector faults (and for extra replicas, which has no trial) the scorer says <i>no opinion</i>. On the held-out windows of the kept kinds the judge "
            f"backed the true claim {J2['true_claims']['agree']} of {sum(J2['true_claims'].values())} times and caught {J2['wrong_claims']['disagree']} of {sum(J2['wrong_claims'].values())} wrong claims. "
            "Two trials of each state fault is little: these figures show how separable the faults are, not a live accuracy."),
          P("Real faults the model had never seen", H2),
          P("The faults were then made real: pool exhaustion leaks every slot of the service's bounded pool, memory holds real memory with real work per request, disk really fills the database's log directory. "
            "Windows from those runs were scored by models that had not trained on them:"),
          table([["Test", "Current model (own.joblib)", "Retrained (own_real, leave one real trial out)", "Wrong claim agreed?"],
                 ["Run coll1: 10 real faults, 40 windows", "40 of 40 service and fault right", "40 of 40", "0 of 40, both models"],
                 ["Run coll2: 8 real faults, 32 windows", "32 of 32", "32 of 32", "not recorded for this run"]],
                [W * 0.30, W * 0.24, W * 0.28, W * 0.18]), Spacer(1, 4),
          P("<b>Retraining on real faults changed nothing measurable</b>: the current model was already right on every window, so there was no headroom to show an improvement. "
            "That is a finding, not a failure: the shipped default stays, and the retrained file is available with <font face='Courier'>SCORER_MODEL=own_real.joblib</font>. "
            "We did not claim it helped.", CALL),
          PageBreak()]

    # ---------------- 9
    s += [P("8. The judge: how a verdict is made and used live", H1),
          P("The scorer's answer (service, fault, confidence) is turned into agree / disagree / no_opinion by a small, pure function. This is the only place the ML output touches a decision:"),
          code("""
def judge(cause, service, scorer_service, scorer_fault, confidence, min_confidence, vocabulary=()):
    if cause in STATE_CAUSES and KIND_OF.get(cause) not in vocabulary:
        return {"verdict": "no_opinion", ...}              # a Kubernetes-state fault whose kind the model does not know
    if confidence < min_confidence:
        return {"verdict": "no_opinion", ...}              # scorer unsure -> say nothing
    if scorer_service != service:
        return {"verdict": "disagree", ...}                # the metrics point somewhere else
    expected = {cause} if cause in vocabulary else ({KIND_OF[cause]} if cause in KIND_OF else CAUSE_FAULTS.get(cause))
    if expected is None:
        return {"verdict": "agree", "basis": "service only", ...}
    if scorer_fault not in expected:
        return {"verdict": "disagree", ...}                # right place, wrong kind of fault
    return {"verdict": "agree", "basis": "service and fault", ...}
"""),
          table([["Verdict", "Dispatcher does", "Why it is safe"],
                 ["agree", "May run the approved playbook (if confidence >= 0.75, not in cooldown, AUTO_REMEDIATE on)", "Two independent views match."],
                 ["disagree", "Escalates to a human, runs nothing", "Metrics and alerts tell different stories."],
                 ["no_opinion", "Escalates for the original three causes; accepted for the Kubernetes-state causes", "Silence is not consent. For a state fault the model cannot see, the rules decide alone, still behind the gate and the recovery check."],
                 ["unavailable", "Escalates", "Service down or slow: the client returns this on any error so it can never make an incident worse."]],
                [W * 0.14, W * 0.50, W * 0.36]), Spacer(1, 4),
          P("What happened live (on the AWS lab cluster, real faults, safe defaults restored and confirmed after each run)", H2),
          table([["Run", "What it showed"],
                 ["coll1 (default model, dry)", "10 of 10 real faults: verdict <b>agree</b>"],
                 ["coll2 (retrained model, dry)", "8 of 8 agree; the two-fault 'pair gate' check passed (the gate vetoed when two causes were present)"],
                 ["comp13 (live)", "memory x3, pool x2, disk x2: 6 passed and 1 escalated by policy because the ML said <b>disagree</b> on a real memory fault"],
                 ["cloud1 / cloud2 (live, cold start)", "pool, memory, disk, unknown cause all passed; unknown cause correctly escalated"],
                 ["final sessions (live, 18 to 19 Nov)", "17 faults and variations recovered; the first down:database and the first memory run were escalated by a <b>disagree</b>, and both recovered afterwards (retrained scorer, re-run)"]],
                [W * 0.27, W * 0.73]), Spacer(1, 4),
          P("The one disagreement", H2),
          P("In comp13 trial 1, and again on the first memory run of the final session, the ML disagreed with a correct memory diagnosis, so the dispatcher escalated instead of acting. That is the safe outcome, and it is the system working as designed, but it was a <i>false alarm</i> by the scorer. "
            "Offline, the same kind of window scores right. The leading hypothesis (not proven) is timing: the offline windows start at the <i>true</i> fault time, while live the engine must <i>estimate</i> it from alert times:"),
          code("""
def estimate_fault_start(alerts, deciding):
    # only the deciding symptoms count; wind each back by its own `for:` delay + a scrape and an evaluation
    estimates = [startsAt(a) - FOR_SECONDS[name] - 15 for a in alerts if name in deciding]
    return min(estimates)
"""),
          P("If that estimate is a little late, the 'fault window' includes some normal behaviour and the z-scores shrink. It is listed as an open item rather than explained away: re-scoring all 36 recorded real memory windows offline never flips the verdict, and the re-runs agreed.", CALL),
          PageBreak()]

    # ---------------- 10
    s += [P("9. Limits, takeaways and how to reproduce", H1),
          P("Honest limits", H2),
          table([["Limit", "Why it matters"],
                 ["Small samples", "About 230 training windows (79 trials); only two trials of each Kubernetes-state fault, and 72 real-fault windows. 'No wrong agreement found' is not 'cannot happen'."],
                 ["We injected the faults", "The faults and the labels come from the same team. Real production incidents may look different. Three resource causes with many trials, but only two trials of each state fault; the scorer has no opinion on selector faults and extra replicas."],
                 ["Rules and simulator share an author", "Simulated accuracy (89.9% / 76.9%) shows behaviour under noise, not real-world accuracy."],
                 ["Confidence is not a probability", "The rules' confidence is a completeness ratio; the scorer's is calibrated on few held-out trials. Neither is a guarantee."],
                 ["Two live false alarms unexplained", "comp13 trial 1 and the first memory run of the final session (both memory). Timing of the estimated fault start is the main suspect; offline re-scoring does not reproduce it and the re-runs agreed, so it is not proven."],
                 ["Public-dataset model", "Right service 91% of the time but right fault only 67%; kept for reference, not used live."]],
                [W * 0.30, W * 0.70]),
          P("What to take away", H2),
          P("1. The ML is a <b>veto</b>, not a decision-maker. It can only cause an escalation, never an action.<br/>"
            "2. It looks at <b>different evidence</b> from the rules (raw metrics, not alerts), which is why its agreement means something.<br/>"
            "3. Training on <b>our own</b> injected faults beat training on a public dataset, because the public data lacked our disk and pool signals and used different fault types.<br/>"
            "4. We measured honestly: leave-one-trial-out, a verdict test with a deliberately wrong claim, real faults as a final check, and we reported that retraining did <b>not</b> help.<br/>"
            "5. It is shown working end to end on real faults, and the disagreements are reported, not hidden.<br/>"
            "6. It keeps only what is reliable: fault kinds below 95% on held-out trials are left out, and the scorer says 'no opinion' about them instead of guessing."),
          P("Reproduce it", H2),
          code("""
# retrain with the Kubernetes-state fault trials; keeps only the kinds that held out at 95% or better (scikit-learn 1.6.1)
python second-opinion/retrain_with_state_faults.py --old docs/live-validation-run3 docs/live-validation-run4 \\
    docs/live-validation-run5-early docs/live-validation-run8 --new docs/completion/state-fault-trials/A \\
    docs/completion/state-fault-trials/A2 docs/completion/state-fault-trials/B

# the first cluster-trained model
python second-opinion/train_own.py docs/live-validation-run3 docs/live-validation-run4 \\
    docs/live-validation-run5-early docs/live-validation-run8

# compare current vs retrained on the real-fault trials
python second-opinion/eval_real.py --old docs/live-validation-run3 ... --real docs/completion/real-fault-trials

# the rule-engine simulation
cd correlation-engine && python evaluation/evaluate.py --difficulty moderate --n 5000 --seed 7

# record new real-fault training data on the lab cluster (decisions only, no remediation)
python scripts/scenarios/run_scenarios.py --out results/run \\
    --scenarios pool,memory,disk,down:database,paused:api-gateway --collect
"""),
          P("Glossary", H2),
          table([["Term", "Meaning"],
                 ["Root cause", "The underlying fault (pool exhausted, memory held, disk full), not a symptom."],
                 ["Symptom / deciding symptom", "An alert that fired. The deciding one is the symptom that distinguishes one cause from the others."],
                 ["Robust z-score", "How far a metric moved from its normal level, in units of its normal wobble, ignoring outliers."],
                 ["Leave one out", "Test on a trial (or system) the model never trained on, repeated for every trial."],
                 ["Calibration (Platt)", "Re-scaling a model's confidence so it better matches how often it is actually right."],
                 ["Escalate", "Hand the incident to a human and run nothing."]],
                [W * 0.28, W * 0.72])]
    doc = SimpleDocTemplate(str(OUT), pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=14 * mm, bottomMargin=16 * mm,
                            title="AutoSRE: the ML code and its results", author="AutoSRE team")
    doc.build(s, onFirstPage=footer, onLaterPages=footer)
    print("wrote", OUT)


if __name__ == "__main__":
    build()
