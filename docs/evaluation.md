# Correlation engine - evaluation

How well does the engine name root causes, how was that measured, and what can and
cannot be concluded from it. Written by the ML role; see `correlation-engine/`.

> **Read this first.** Every number below, except the [Live validation](#live-validation)
> section, was measured on **simulated** incidents. They
> show how the engine behaves when symptoms drop out, cascade and get buried in noise.
> They are **not** an accuracy figure for real banking incidents, because the simulator
> and the signatures were written by the same person. Real numbers need incidents
> captured from a live cluster - the small run in [Live validation](#live-validation) is a
> start, not a rate.

## What the engine is

A deterministic, rule-based expert system. **There is no trained model in the shipped
engine.** The knowledge is hand-encoded: the dependency chain (`dependency_graph.py`),
one signature per root cause (`signatures.py`), and the arithmetic in `correlate.py`.

The brief calls for this ("rule-based, not ML, unless time allows"), and it is also the
defensible choice for a bank: the engine produces an auditable sentence such as

> transaction-service is the deepest alerting service in the chain; its deciding symptom
> DBPoolNearExhaustion fired; 4 of 4 expected symptoms present (...); confidence 100%.

A learned score of 0.91 explains nothing to an auditor. A classifier was still trained as
the brief's stretch goal, to see whether it would be better - it was not safer
([below](#would-a-learned-model-be-better)).

## The confidence model

The brief suggests `matched / expected`. Over four symptoms that can only produce
25 / 50 / 75 / 100 %, and it treats the deciding symptom as no more informative than a
downstream consequence. Each symptom instead carries a weight:

    confidence = sum(weights of symptoms that fired) / sum(all weights)

| cause | deciding symptom | consequences |
|---|---|---|
| `db_connection_pool_exhaustion` | DBPoolNearExhaustion **0.55** | ErrorRateHigh 0.20, PodRestartsHigh 0.15, APILatencyHigh 0.10 |
| `high_memory_usage` | ContainerMemoryHigh **0.60** | PodRestartsHigh 0.25, APILatencyHigh 0.15 |
| `db_disk_full` | DBDiskNearFull **0.60** | ErrorRateHigh 0.25, APILatencyHigh 0.15 |

The deciding symptom carries most of the weight because it is the one that distinguishes
this cause from every other; the rest are consequences that several causes share. The
score is still a plain ratio that can be printed back as a sentence.

Consequences:

- A cause cannot be named at all unless its deciding symptom fired **on the service that
  owns it** (`required_symptoms`). Confidence only ranks matches; it never creates one.
- A lone deciding symptom scores 0.55-0.60, below the dispatcher's planned 0.75 gate, so
  it escalates rather than acts.
- The brief's example of "91%" is reachable only by luck of weights. The demo scenario
  (all four symptoms) scores **100%**; losing the latency symptom scores **90%**. Put the
  number the engine actually produces on the slide.

## How the engine decides

1. Alertmanager groups alerts into one incident (`group_wait: 30s`) - algorithm step 1 is
   configuration, not code.
2. Map each alert's `service` label to its depth in the chain; ignore unknown services.
3. The **deepest** alerting service is the candidate root.
4. A signature matches only if its deciding symptom fired on that service.
5. Score it. If nothing matches, return `None`, which means **escalate to a human**.

**It fails safe.** If the deepest alerting service has no matching signature, the engine
escalates; it does *not* fall back to a shallower service that does match. A deeper
service alerting for an unknown reason is the likeliest true cause, and acting on a
shallower one would restart the wrong thing. Whether that is the right trade is measured
[below](#fail-safe-versus-fall-back) and turns out to depend on an unknown.

## How it was measured

```bash
cd correlation-engine
pip install -r requirements-dev.txt
python evaluation/evaluate.py --difficulty moderate --n 5000 --seed 7
python evaluation/evaluate.py --difficulty hard     --n 5000 --seed 7

pip install -r requirements-ml.txt                  # classifier only
python evaluation/train_classifier.py
```

The simulator (`evaluation/simulate.py`) builds incidents of five kinds: one per known
cause, an `unknown` fault no signature describes, and a `deep_fault` (the database
degrades and starves the transaction service's pool - the pool alerts are real but are a
*symptom*, so restarting the transaction service is the wrong fix). Symptoms drop out,
errors cascade up the chain, and noise includes unrelated alerts and occasionally a
different cause's deciding symptom (a *confounder*). The label is the cause that was
injected.

Plain accuracy hides what matters, so outcomes are split four ways, and the headline is
the **wrong-action rate**:

| outcome | meaning | cost |
|---|---|---|
| correct action | named the true cause | none |
| correct escalation | no known cause, and it escalated | none |
| missed | a known cause existed, but it escalated | a human's time |
| **wrong action** | named a cause that was not the true one | a human's time **and** a wrong playbook may have run |

Both of the above run against the engine exactly as shipped.

## Results

Full output for both noise levels is in the [appendix](#appendix-full-output). Headlines:

| | moderate noise | hard noise |
|---|---:|---:|
| accuracy | **89.9%** | **76.9%** |
| correct actions | 64.3% | 51.5% |
| missed (escalated a known cause) | 8.7% | 17.6% |
| **wrong actions** | **1.5%** | **5.5%** |

Clean data (every symptom, no noise) gives 100% accuracy, which is the expected sanity
check and is enforced by a test.

### Where the failures come from

Every failure is attributed to a cause using metadata the simulator records, and the
"unexplained" bucket is asserted empty by a test - if the simulator and engine ever
disagree about something, the build breaks instead of the numbers quietly drifting.

- **Misses** come from two sources: the deciding symptom never fired (an alert flapped),
  and an unrelated alert on a deeper service blocked the match. Both are the engine
  declining to guess, which is by design.
- **Every wrong action** in both runs came from one situation: a second cause's deciding
  symptom was also present. Two independent deciding symptoms firing together means two
  faults, or one fault plus a misleading signal; the engine picks one by score.

### The confidence score does not catch the wrong actions

| | correct | wrong |
|---|---:|---:|
| mean confidence, moderate | 0.93 | 0.84 |
| mean confidence, hard | 0.86 | 0.81 |

A wrong call looks almost as confident as a right one, because confidence measures how
*complete* the symptom set is, not whether the diagnosis is right. A confounded incident
can look complete. In practice:

- a gate of **0.75** (the planned dispatcher default) removes few wrong actions - on hard
  data 275 become 220;
- a gate of **0.90** cuts them to 82 but also cuts automation to 25% of incidents.

**The gate is weaker protection than it appears.** Do not describe it to the examiner as
a safety mechanism without this caveat.

### Fail safe versus fall back

The engine's refusal to act when the deepest alerting service is unexplained is a design
choice, so it was tested against the obvious alternative (walk up to a shallower match).
On the moderate data:

| engine | correct actions | missed | wrong actions |
|---|---:|---:|---:|
| fail safe (shipped) | 3214 | 434 | 73 |
| fall back to shallower | 3468 | 179 | 455 |

That 6x difference in wrong actions **depends entirely on how often deeper faults occur**,
which is an assumption (8% in the simulator), not a measurement. Varying it:

| deep-fault share | engine | correct actions | missed | WRONG ACTION rate | wrong actions (count of 20,000) |
|---:|---|---:|---:|---:|---:|
| 0% | fail safe | 69.9% | 9.0% | 1.7% | 336 |
| 0% | fall back | 75.1% | 3.7% | 1.7% | 345 |
| 1% | fail safe | 69.0% | 8.9% | 1.6% | 325 |
| 1% | fall back | 74.2% | 3.7% | 2.7% | 534 |
| 2% | fail safe | 68.5% | 8.8% | 1.7% | 332 |
| 2% | fall back | 73.7% | 3.6% | 3.6% | 714 |
| 4% | fail safe | 66.8% | 8.8% | 1.6% | 316 |
| 4% | fall back | 72.0% | 3.5% | 5.4% | 1076 |
| 8% | fail safe | 64.3% | 8.3% | 1.6% | 312 |
| 8% | fall back | 69.2% | 3.4% | 9.1% | 1821 |

Reading it honestly:

- With **no** deeper faults, fall-back is strictly better: about 5 points more automation
  and the same wrong actions.
- Every additional 1% of deeper faults adds roughly 1 point of wrong actions to fall-back,
  while fail-safe stays flat at 1.6-1.7%.
- So fail-safe costs about 5 points of automation to avoid roughly 1 wrong action per
  100 incidents for each 1% of deeper faults.

Which is better is a **judgement about cost**, not something the data settles: how
expensive is a wrong playbook on payment infrastructure compared with paging an engineer?
For a banking system fail-safe is the defensible default, and it is what ships - but the
team should say that it is a judgement and know the real deeper-fault rate is unmeasured.

### Would a learned model be better?

The brief's stretch goal: a scikit-learn decision tree trained on labelled incidents
(labels are free - every injected fault is known).

Trained on 8000 simulated 'moderate' incidents (seed 1), max depth 8, 86 leaves. Tested on fresh incidents from different seeds.

**SIMULATED data throughout - see the caveat at the top of this file.**

| test set | model | accuracy | correct actions | missed | WRONG ACTIONS |
|---|---|---:|---:|---:|---:|
| moderate, new seed | hand-written rules | 90.5% | 64.0% | 8.0% | 1.5% |
| moderate, new seed | decision tree | 94.9% | 69.8% | 2.6% | 2.5% |
| hard (harder than training) | hand-written rules | 77.8% | 51.9% | 17.6% | 4.6% |
| hard (harder than training) | decision tree | 85.1% | 61.1% | 8.7% | 6.2% |

#### What the tree relied on

| feature | importance |
|---|---:|
| DBDiskNearFull@database | 0.325 |
| ContainerMemoryHigh@transaction-service | 0.310 |
| DBPoolNearExhaustion@transaction-service | 0.169 |
| deepest_service_depth | 0.116 |
| PodRestartsHigh@transaction-service | 0.020 |
| ErrorRateHigh@transaction-service | 0.016 |
| APILatencyHigh@api-gateway | 0.013 |
| ContainerMemoryHigh@database | 0.011 |

The full tree can be printed with `python evaluation/train_classifier.py`.

The tree wins on accuracy and on misses, and **loses on the metric that matters**: it
makes more wrong actions than the rules, on both test sets. It learned the simulator's
noise statistics and acts despite unexplained deeper alerts - the same trade as the
fall-back variant. Some of its splits are on `n_alerts`, a quirk of how the simulator
happens to count alerts that would not carry over to a real cluster.

> **Correction to an earlier prediction.** Before running this I expected the tree to
> "rediscover the hand-written rules and score near-perfectly". It did not: it scored
> higher on accuracy and lower on safety. Accuracy alone would have called the tree the
> winner.

The rules therefore stay the default. A tree trained on simulated data can only learn the
simulator back, so this comparison is only meaningful once repeated on captured incidents.

## Limitations

- **Simulated data.** Symptom behaviour, noise rates and the mix of incident kinds are all
  assumptions. The deeper-fault share in particular is unknown and drives one of the
  findings.
- **Same author.** The signatures and the simulator share a mental model, so some
  agreement between them is guaranteed.
- **Three causes.** With so few, a hand-written table is hard to beat. The case for a
  learned model strengthens only as the number of causes grows.
- **The metrics are synthetic.** Timing figures (microseconds per incident) are real, but
  say nothing about end-to-end remediation time, which is dominated by the 30s
  `group_wait` and the playbook run.
- **Confidence is uncalibrated.** It is a completeness ratio, not a probability, and
  should not be presented as one.

## Recommendations

1. **Treat two simultaneous deciding symptoms as ambiguity and escalate.** All remaining
   wrong actions come from exactly this. It is *not* implemented because the simulator
   creates confounders by exactly this mechanism, so adding the rule would show zero wrong
   actions by construction - a number that proves nothing. It is a sound safety rule on its
   own merits; the cost is more escalations when two real faults coincide. Decide as a
   team.
2. **Measure the real deeper-fault rate** before relying on the fail-safe finding.
3. **Set the dispatcher's confidence gate with the caveat above.** 0.75 is a sanity floor,
   not a safeguard.
4. **Read the live validation** below alongside the simulated figures; it is a small, single-cause, injected-fault run.

## Live validation

Run on 2026-10-31 on a single `t3.large` (minikube + kube-prometheus-stack), through the real
Prometheus → Alertmanager → engine pipeline. The faults were **injected** through the
`CHAOS_ENABLED` endpoints; everything downstream of the injection is real. So this measures
detection and correlation on staged faults - not whether real failures look like these.
The full generated report is [`live-validation/report.md`](live-validation/report.md), with
per-trial records in [`trials.jsonl`](live-validation/trials.jsonl).

**Sample size is small - treat it as anecdote, not a rate.** 18 trials were run, 17 usable
(trial 3 errored in the injector, see below): 4 pool exhaustion, 5 disk full, 5 memory
pressure, 3 "unknown" faults the engine should escalate.

### Result: the first webhook of each trial (what a dispatcher would act on)

| outcome | count |
|---|---:|
| correct action | 14 |
| correct escalation (unknown faults) | 3 |
| missed (escalated a known cause) | 0 |
| **wrong action** | **0** |

No wrong action in 17 trials. With n this small, the honest reading is "no counter-example
found", not "100% accurate": the faults were written by the same people who wrote the
signatures, each one isolates a single cause, and the three unknown faults were all
consequence-only (gateway latency or errors), so the live run never exercised the case the
simulation flagged as dangerous - a deeper fault with no signature, or two causes at once.

### Timing and the confidence gate

- A first incident reached the engine a median **83 s** after injection (range 60-140 s):
  15 s scrape + `for:` + the 30 s Alertmanager `group_wait`.
- The deciding symptom alerts **first and alone**, so the first webhook usually scores only
  0.55-0.60 (`DBPoolNearExhaustion` 0.55, `ContainerMemoryHigh`/`DBDiskNearFull` 0.60).
  A dispatcher gated at 0.75 would therefore wait for a later webhook: it acted at a median
  **138 s**, and in **4 of 14** known-cause trials (#2, #5, #6, #11) it never reached the
  gate at all. The 0.75 gate trades speed - and some automation - for caution; pool
  exhaustion at 19/20 connections produces too few consequences to clear it.
- A trial takes 8-10 minutes, not 6, because the rules use `rate(...[5m])`: latency and error
  alerts keep firing for about five minutes after a fault ends.

### The "every record" figure is not an accuracy

The report also scores every webhook, giving 61.5% with 20 "missed". That number is an
artefact and should not be quoted as accuracy: once the deciding alert clears, the lingering
consequence alerts (`APILatencyHigh`, `ErrorRateHigh`) still produce webhooks, and with the
cause gone the engine correctly escalates them. It shows the engine failing safe, and that a
dispatcher must treat "the cause already cleared" updates as noise rather than new incidents.

### What running it live found that the simulator could not

1. **Alerts never reached the engine.** `sum by (service)` drops the `namespace` label, and
   Alertmanager only routes `namespace = "autosre"`. Every rule now sets it explicitly, and a
   test enforces it.
2. **Grafana was OOM-killed** (256Mi limit); raised to 768Mi.
3. **The memory injector killed its own target.** `kubectl exec` runs inside the container's
   memory cgroup, so the injector's overhead pushed the pod over its limit (exit 137, trial
   3). Injection now goes through the API-server proxy.
4. **The dashboard's "Escalated to a human" was wrong.** Resolved notifications were counted
   as failed diagnoses (48 shown after a handful of real escalations). Fixed with a
   regression test.

### Dashboard (M7)

All 13 panel queries return data on the live cluster (`live-validation/dashboard_check.txt`),
including the confidence panel with the 0.75 gate marked.

![Decisions](images/dashboard-decisions.jpg)
![Symptoms](images/dashboard-symptoms.jpg)

### Not in the repo

`incidents.jsonl` (the raw 52 captured webhooks, 223 KB) lives only in the S3 bucket
`autosre-validation-841736140404`; `report.md` is derived from it by
`evaluation/live_report.py`. Download it before deleting the bucket if you want to reproduce
the report.

## Third-party benchmark: RCAEval RE1

The simulation and the live run were both written by us. To test the approach on data we did
not write, we used [RCAEval](https://github.com/phamquiluan/RCAEval) RE1: 375 real fault
injections (373 loaded) across three systems - Online Boutique, Sock Shop, Train Ticket -
with five fault types (cpu, mem, disk I/O, delay, loss) and the faulty service labelled.
Full tables: [`rcaeval-results.md`](rcaeval-results.md). Code: `evaluation/rcaeval_eval.py`.

RE1 is raw 1-second metrics, not alerts, so each metric is scored by how far the first five
minutes after injection sit from its own normal behaviour, and those scores replace "which
alerts fired". Two diagnosers see only those scores:

- **Replay baseline** - rules in the style of `correlate.py`, written before looking at the
  results: resource symptoms outrank latency; latency with errors is loss, latency alone is
  delay; nothing firing escalates. Only the threshold was chosen, on Online Boutique, and
  then frozen.
- **Learned model** - a random forest ranks services as the root cause, then a calibrated
  gradient-boosted classifier names the fault. Features are system-agnostic, and the
  evaluation is **leave-one-system-out**, so every test system is unseen in training.

| | wrong action | right action | escalates |
|---|---:|---:|---:|
| Replay baseline (373 cases) | 106 (28%) | 138 (37%) | 129 (35%) |
| Learned, always acts | 145 (39%) | 228 (61%) | 0 |
| Learned, acts at confidence >= 0.5 | 60 (16%) | 133 (36%) | 180 (48%) |

**Read this as a modest result, not a win.**

- **The learned model only beats the rules on coverage.** Forced to act on everything it is
  right 61% of the time and wrong 39%, which is worse than the rules' wrong-action rate.
  Gated at 0.5 it matches the rules' right actions with fewer wrong ones (16% vs 28%), but
  the gate is blunt: calibration error fell from 0.20 to 0.12 after fitting a calibrator on
  out-of-fold predictions, yet confidence still barely separates right from wrong at the
  high end (14% precision at 0.7, on 7 cases).
- **Most errors are confusions the data cannot resolve.** Service localisation is good
  (about 90% top-1). The fault type is not: cpu is called disk 48 times out of 74, disk is
  called cpu 21 times, and delay and loss swap places 22 and 21 times. RE1 has no disk
  metric and its error metrics never move, so CPU stress and disk I/O stress look alike, and
  so do delay and loss. More modelling will not fix missing signal.
- **The baseline's wrong assumptions are informative.** Its "latency plus errors means loss"
  rule was wrong for this data (errors stay flat), so it named no loss correctly. The rules
  also fail across systems: Online Boutique (where the threshold was set) shows 13 wrong
  actions, Sock Shop 60. A threshold chosen on one system does not transfer.
- **Memory faults are the clear success.** 99% right in the learned model, because memory
  stress is the one fault with its own distinct signature.
- **The tiny improvements were not kept.** Variability features gave no reliable gain on the
  development systems (Online Boutique and Sock Shop) and are off by default.

What this does *not* show: RE1's disk faults are I/O stress, not "disk full", and there is
no pool-exhaustion equivalent, so these numbers say nothing about our three causes in
particular. The faults are also injected for 35 minutes into otherwise clean systems. Two
disclosures on method: the threshold grid was widened after the first run because every
value then sat at its upper edge, and Train Ticket's numbers were visible during that
iteration; later design decisions used only Online Boutique and Sock Shop.

### RE2: adding disk-I/O and socket metrics, and logs

RE2 (270 cases: 15 per fault per system, six faults including a socket fault) has two metrics
RE1 lacked, `diskio` and `socket`, plus container logs. The rules were extended before
running (a disk-I/O symptom means disk, a socket symptom means socket); the "latency plus
errors means loss" rule was left as it was, although RE1 had already shown it wrong.
Tables: [`rcaeval-re2-results.md`](rcaeval-re2-results.md) (metrics) and
[`rcaeval-re2-logs-results.md`](rcaeval-re2-logs-results.md) (metrics plus logs).

| | wrong action | right action | escalates |
|---|---:|---:|---:|
| Replay baseline (270 cases) | 129 (48%) | 136 (50%) | 5 (2%) |
| Learned, always acts | 98 (36%) | 172 (64%) | 0 |
| Learned, acts at confidence >= 0.6 | 32 (12%) | 83 (31%) | 155 (57%) |
| Learned, acts at confidence >= 0.9 | 2 (1%) | 9 (3%) | 259 (96%) |

- **The new metrics helped where the signal exists.** Disk faults rose from confused with CPU
  (65%) to 78% right once `diskio` was available, and the fault is now properly separable
  from CPU stress. Confidence also became usable: precision of acted-on cases rises with the
  gate (69% at 0.5, 72% at 0.6, 82% at 0.9), the first time it has behaved like a gate.
- **The rules got worse, not better.** Adding symptoms to hand-written rules without
  fitting them produced more wrong actions (48%), because the symptoms overlap: memory
  stress also moves disk I/O, and CPU stress moves sockets. This is the clearest case so far
  for learning the weights rather than writing them.
- **Logs did not help.** Adding log-volume and error-line features gave 63.0% right, against
  63.7% without. The features are crude (volume and keyword counts), so this says logs *as
  summarised here* add nothing, not that logs cannot help. Traces (2.5 GB, not downloaded)
  were skipped for the same reason.
- **Remaining confusions are physical overlaps.** Memory stress is called disk 15 times in 45
  (it drives I/O), delay and loss swap 8-12 times, and CPU and socket swap 7-11 times.

## Second opinion from raw metrics

The rule engine reasons over alert *names*. A second service, `second-opinion/`, looks at the
metrics underneath them and says whether it agrees on where the fault is. It is wired in as
a check on the rules' decision, not as a second decision-maker.

**How it works.** After the rules name a cause, the engine POSTs `{root_cause, service,
incident_start}` to the scorer. The scorer pulls the incident window from Prometheus (cpu,
memory, p95 latency, 5xx ratio, request rate for each of the four services), scores each
service against its own earlier behaviour, and a model trained on RCAEval RE1 names the most
likely faulty service and fault type. The verdict is `agree`, `disagree` or `no_opinion`.

**Why a separate service.** scikit-learn does not fit in the engine's 128 Mi limit, and a
separate read-only service keeps the engine pure and lets the scorer fail without taking the
decision path down. The engine calls it with a short timeout and treats any failure as
`unavailable`: a dead, slow or confused scorer never blocks an incident.

**Modes** (`SECOND_OPINION_MODE` on the engine; the URL being unset turns it off):

| mode | effect |
|---|---|
| `shadow` (default) | the verdict is added to the webhook response and counted in `second_opinion_verdicts_total`; the decision is unchanged |
| `gate` | on `disagree` the engine withholds the action and escalates (`status: vetoed`, counted in `vetoed_incidents_total`); `agree`, `no_opinion` and `unavailable` all proceed |

**What it can and cannot confirm.** The model's fault vocabulary is RCAEval's (cpu, mem, disk
I/O, delay, loss), which does not match ours:

- `high_memory_usage` is checked on service **and** fault type (`mem`, which the model gets
  99% right on held-out systems).
- `db_disk_full` and `db_connection_pool_exhaustion` have no matching fault type, so only the
  **service** is compared. That is still independent evidence, since it uses raw metrics
  rather than the alerts, but a weaker check. The scorer cannot see disk usage or pool
  gauges at all.

**How good is the model?** It is trained on RE1 only, because RE1 has exactly the five
signals our cluster exposes, and without features that depend on how many services a system
has (RCAEval's have 12-100+, ours has four). Leave-one-system-out on 373 cases:

| | service top-1 | fault right | both right |
|---|---:|---:|---:|
| all services present | 90.9% | 66.8% | 61.4% |
| cut down to four services | 95.4% | 71.0% | 68.4% |

Naming the *service* is the strong part, which is what the check mostly relies on.

**What is not validated.** This is a transfer from three open-source demo systems to ours,
and none of the following has been run:

- The scorer has never been run against our cluster; the Prometheus queries, the
  `container`/`service` label assumptions and the `second-opinion` image have only been tested
  against fakes (13 service tests, 8 engine tests; five deliberately broken variants were all
  caught). Docker was not available where this was built.
- Our faults are staged by chaos endpoints in a 15-second-scrape setup, unlike RCAEval's
  1-second data, and the incident-start estimate is "earliest alert minus two minutes".
- Whether it *helps* is unknown. That is why `shadow` is the default.

**To validate it,** deploy it in `shadow` mode, rerun the live validation
(`docs/aws-validation.md`), and compare each trial's verdict with its known label. Count how
often it agrees on correct diagnoses and how often it would have caught a wrong one; only
then consider `gate`. `vetoed_incidents_total` is not yet charted: the dashboard's
"Escalated to a human" panel counts only unmatched incidents, so it would undercount vetoes
in gate mode.

## Appendix: full output

Regenerate with the commands under "How it was measured". Seed 7, 5000 incidents each.

### Simulated incidents - moderate

SIMULATED (seed 7). Noise: deciding symptom seen 95%, each consequence 80%, error cascades 70%, unrelated alerts 25%, confounding cause 5%. Measures robustness to noise, not real-world accuracy.

**5000 incidents.** Accuracy: **89.9%**

#### Outcomes

| outcome | count | share |
|---|---:|---:|
| correct action | 3214 | 64.3% |
| correct escalation | 1279 | 25.6% |
| missed (escalated a known cause) | 434 | 8.7% |
| WRONG ACTION | 73 | 1.5% |

#### Confusion matrix

| true \ predicted | db connection pool exhaustion | db disk full | high memory usage | escalate |
|---|---:|---:|---:|---:|
| **db connection pool exhaustion** | 1045 | 34 | 5 | 186 |
| **db disk full** | 2 | 1193 | 2 | 59 |
| **high memory usage** | 1 | 29 | 976 | 189 |
| **escalate** | 0 | 0 | 0 | 1279 |

#### Per class

| class | precision | recall | F1 | support |
|---|---:|---:|---:|---:|
| db_connection_pool_exhaustion | 99.7% | 82.3% | 90.2% | 1270 |
| db_disk_full | 95.0% | 95.0% | 95.0% | 1256 |
| high_memory_usage | 99.3% | 81.7% | 89.6% | 1195 |
| escalate | 74.7% | 100.0% | 85.5% | 1279 |

#### What a confidence gate buys

An action is taken only if a cause was named *and* confidence >= the gate; everything else escalates to a human.

| min confidence | automated actions | wrong actions | precision of actions | known incidents auto-remediated |
|---:|---:|---:|---:|---:|
| 0.00 | 3287 (65.7%) | 73 (1.5%) | 97.8% | 86.4% |
| 0.50 | 3287 (65.7%) | 73 (1.5%) | 97.8% | 86.4% |
| 0.75 | 3150 (63.0%) | 69 (1.4%) | 97.8% | 82.8% |
| 0.90 | 2186 (43.7%) | 27 (0.5%) | 98.8% | 58.0% |

#### Why the failures happened

**Missed (escalated a known cause)**

| reason | count |
|---|---:|
| an unrelated alert on a deeper service blocked the match | 255 |
| the deciding symptom never fired (alert flapped or was missed) | 179 |

**Wrong action**

| reason | count |
|---|---:|
| a second cause's deciding symptom was also present | 73 |

#### Design check: fail safe vs fall back

The engine escalates when the deepest alerting service has no matching signature. The alternative walks up to a shallower service that does match. Same data, both engines:

| engine | correct actions | missed | WRONG ACTIONS | wrong-action rate |
|---|---:|---:|---:|---:|
| fail safe (shipped) | 3214 | 434 | 73 | 1.5% |
| fall back to shallower | 3468 | 179 | 455 | 9.1% |

The fall-back variant recovers some missed incidents, and pays for it with wrong actions on incidents where a deeper fault is the real cause. Which wins depends on how often deeper faults actually occur - the share used here is an assumption (`simulate.KIND_MIX`), not a measurement.

#### Diagnostics

- mean confidence when correct: 0.93
- mean confidence when WRONG: 0.84 (73 cases)
- engine time per incident: 4 microseconds

### Simulated incidents - hard

SIMULATED (seed 7). Noise: deciding symptom seen 85%, each consequence 60%, error cascades 50%, unrelated alerts 50%, confounding cause 15%. Measures robustness to noise, not real-world accuracy.

**5000 incidents.** Accuracy: **76.9%**

#### Outcomes

| outcome | count | share |
|---|---:|---:|
| correct action | 2574 | 51.5% |
| correct escalation | 1270 | 25.4% |
| missed (escalated a known cause) | 881 | 17.6% |
| WRONG ACTION | 275 | 5.5% |

#### Confusion matrix

| true \ predicted | db connection pool exhaustion | db disk full | high memory usage | escalate |
|---|---:|---:|---:|---:|
| **db connection pool exhaustion** | 802 | 91 | 32 | 382 |
| **db disk full** | 4 | 1043 | 13 | 167 |
| **high memory usage** | 29 | 106 | 729 | 332 |
| **escalate** | 0 | 0 | 0 | 1270 |

#### Per class

| class | precision | recall | F1 | support |
|---|---:|---:|---:|---:|
| db_connection_pool_exhaustion | 96.0% | 61.4% | 74.9% | 1307 |
| db_disk_full | 84.1% | 85.0% | 84.6% | 1227 |
| high_memory_usage | 94.2% | 61.0% | 74.0% | 1196 |
| escalate | 59.0% | 100.0% | 74.2% | 1270 |

#### What a confidence gate buys

An action is taken only if a cause was named *and* confidence >= the gate; everything else escalates to a human.

| min confidence | automated actions | wrong actions | precision of actions | known incidents auto-remediated |
|---:|---:|---:|---:|---:|
| 0.00 | 2849 (57.0%) | 275 (5.5%) | 90.3% | 69.0% |
| 0.50 | 2849 (57.0%) | 275 (5.5%) | 90.3% | 69.0% |
| 0.75 | 2446 (48.9%) | 220 (4.4%) | 91.0% | 59.7% |
| 0.90 | 1262 (25.2%) | 82 (1.6%) | 93.5% | 31.6% |

#### Why the failures happened

**Missed (escalated a known cause)**

| reason | count |
|---|---:|
| the deciding symptom never fired (alert flapped or was missed) | 486 |
| an unrelated alert on a deeper service blocked the match | 395 |

**Wrong action**

| reason | count |
|---|---:|
| a second cause's deciding symptom was also present | 275 |

#### Design check: fail safe vs fall back

The engine escalates when the deepest alerting service has no matching signature. The alternative walks up to a shallower service that does match. Same data, both engines:

| engine | correct actions | missed | WRONG ACTIONS | wrong-action rate |
|---|---:|---:|---:|---:|
| fail safe (shipped) | 2574 | 881 | 275 | 5.5% |
| fall back to shallower | 2959 | 473 | 625 | 12.5% |

The fall-back variant recovers some missed incidents, and pays for it with wrong actions on incidents where a deeper fault is the real cause. Which wins depends on how often deeper faults actually occur - the share used here is an assumption (`simulate.KIND_MIX`), not a measurement.

#### Diagnostics

- mean confidence when correct: 0.86
- mean confidence when WRONG: 0.81 (275 cases)
- engine time per incident: 4 microseconds
