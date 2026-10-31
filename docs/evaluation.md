# Correlation engine - evaluation

How well does the engine name root causes, how was that measured, and what can and
cannot be concluded from it. Written by the ML role; see `correlation-engine/`.

> **Read this first.** Every number below was measured on **simulated** incidents. They
> show how the engine behaves when symptoms drop out, cascade and get buried in noise.
> They are **not** an accuracy figure for real banking incidents, because the simulator
> and the signatures were written by the same person. Real numbers need incidents
> captured from the live cluster - see [Live validation](#live-validation-not-yet-run).

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
4. **Run the live validation** below and replace the simulated headline numbers.

## Live validation (not yet run)

This needs the cluster, so it has **not** been done. The tooling is ready:

```bash
# on the cluster, enable capture on the engine
kubectl -n autosre set env deploy/correlation-engine CAPTURE_PATH=/tmp/incidents.jsonl
kubectl -n autosre port-forward deploy/correlation-engine 8000:8000 &

# before EACH fault injection, tell the engine the ground truth
curl -X PUT localhost:8000/capture/label -H 'content-type: application/json' \
     -d '{"label": "db_connection_pool_exhaustion"}'
#   ...inject the fault, wait for the incident to arrive and resolve...
# use {"label": null} for an incident the engine should escalate

# repeat each fault at least 10 times, then evaluate
kubectl -n autosre cp deploy/correlation-engine:/tmp/incidents.jsonl ./incidents.jsonl
python evaluation/evaluate.py --replay incidents.jsonl
```

Report that output **separately** from the simulated figures, with the number of
incidents. Anything under about 30 per cause is an anecdote, not a rate.

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
