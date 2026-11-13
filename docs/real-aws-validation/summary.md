# Real AWS end-to-end validation (first session)

The RCA pipeline tested against real failures of the lab's own resources, with no chaos endpoint and no event sent to
the engine. Session 2026-11-13, lab instance `i-04f8f4d75378a1aba`, scorer `b565d77f`, **as deployed**: gate in
`shadow`, `ESCALATE_ON_AMBIGUITY` off, services at the manifests' 200m CPU limits. Raw results:
`s3://autosre-validation-841736140404/results-real1/` to `results-real5/`. Driver: `real_aws_validate.py`; numbers from
`real_aws_report.py` (the scoring rules are written at the top of that file before any data existed).

Each fault is an ordinary action on a real resource: a real memory-holding process inside the transaction-service
container, scaling the database deployment to zero, a real client pod flooding the frontend, a repeated kill of PID 1.
The engine is never told what was caused; the truth is only in the trial record.

## Headline

**REAL AWS END-TO-END ACCURACY = 100% (5 of 5 scored single-fault trials), 95% Wilson interval 57% to 100%.**

How X was computed: scored single-fault trials in which the rule engine's *first* diagnosis was right (it named the
true cause for a known-cause failure, or escalated for a failure with no known cause), divided by all scored
single-fault trials. A trial is scored only if its baseline was healthy and the real failure was measured as
effective (a real metric crossed a threshold). Two-fault trials and healthy windows are not in the number.

**This is not a claim of 100% accuracy.** Five trials of two failure types cannot support one: the interval's lower
bound is 57%. The scored types are the easy ones (see "What the 100% leaves out").

| Failure type | Trials | Correct | Missed | Wrong | Escalated | Accuracy |
|---|---:|---:|---:|---:|---:|---:|
| memory_pressure (known cause) | 3 | 3 | 0 | 0 | 0 | 100% (3/3) |
| db_outage (no known cause: escalate) | 2 | 2 | 0 | 0 | 2 | 100% (2/2) |
| pool_exhaustion | 1 run | not scored: the real failure never took hold | | | | |
| pod_crash | 1 run | not scored: the real failure never took hold | | | | |
| cpu_pressure, network_partition | 0 | did not run (out of lab time) | | | | |

| Metric | Real AWS result |
|---|---:|
| End-to-end accuracy (first decision) | 100% (5/5) |
| Correct diagnoses / correct escalations | 5 |
| Misses (no webhook reached the engine) | 0 of 5 |
| Wrong diagnoses | 0 of 5 |
| Wrong actions | 0 of 5 (no dispatcher is deployed; "action" is the playbook the engine would hand over) |
| Escalations (first decision) | 2 of 5 (both were the correct outcome) |
| Gate disagreements (shadow) | 0 of 6 named decisions |
| Ambiguity escalations the rule would make | 0 |
| Successful recoveries | 8 of 8 |
| Precision / recall / F1 | 1.00 for both classes present (`high_memory_usage` support 3, `none` support 2) |
| Median detection / decision latency | 124 s (n=5) / 114 s (n=5); decision is timed from fault start, detection from just before it |
| Median recovery latency | 294 s (n=7 at the time of the first read; 8 recoveries in all) |

## Healthy period (no fault)

15 minutes with nothing done: **0 false positives, 0 false escalations, 0 false actions, no alert fired**. One window
at the deployed limits; not enough to say the false-positive rate is zero. (An earlier finding stands: at 200m the idle
gateway p95 sits near the 0.5 s `APILatencyHigh` line, and the baseline check was held up for 22 minutes before the
two-fault trial.)

## Two real faults at once

One trial, memory pressure then a real database outage (both effective):

- The engine named `high_memory_usage` (playbook `scale_replicas.yml`) on 3 webhooks and escalated on the last 2
  (5 webhooks), while the database was down. The scorer **agreed** with memory every time and never saw the second
  fault.
- The database outage, the actual cause of the errors, was not diagnosed; the engine acted on the one failure it has a
  signature for. With a real unrelated failure in progress, the "act on one" behaviour of `ESCALATE_ON_AMBIGUITY=false`
  sent a recommendation that would not have fixed the incident.
- **`ESCALATE_ON_AMBIGUITY` would not have fired**: it needs two *known* causes' deciding symptoms together, and this
  pair has one. So the rule would have changed nothing here (0 ambiguity webhooks).

A real test of the rule on two known causes was **not possible**: the disk signal is a fixed value (a real disk fill
is invisible to this pipeline) and the real client surge only took the pool to 0.65 of its limit (needed 0.9).

## What did not work, exactly

1. **`pool_exhaustion`:** the first design (cut the database's CPU to 10m) was rejected by Kubernetes (limit below the
   request), then, with the request lowered, the database pod could not become ready in 240 s. Both were driver
   faults, not system results. The version that ran, a real client surge, caused an incident and four escalations
   but peaked at 0.65 of the pool. Not scored.
2. **`pod_crash`:** killing PID 1 from inside the container produced no crash loop (restarts never reached the
   threshold) and no webhook arrived. The likely reason is that the kernel ignores signals sent to a namespace's init
   from inside that namespace; that was not verified. Not scored.
3. **`cpu_pressure`, `network_partition`, memory plus CPU:** never started; the lab's remaining time went on 1 and 2.
4. **Disk and stopping the EC2 instance:** not testable end to end (see the driver's docstring): the disk metric is
   not real, and the instance hosts the monitoring being tested.
5. One two-fault trial (memory plus the surge) was lost when I stopped the harness mid-trial.

## Answers

1. **How well did it diagnose real AWS failures?** For the two types that ran to completion, correctly, every time
   (5 of 5). That is two of the six real failure types planned, and only one of the three causes the engine knows.
2. **Wrong actions?** None among the 5 scored trials. In the two-fault trial it acted on the memory fault while the
   database was down, a recommendation that did not address the actual outage.
3. **Missed?** None of the scored trials.
4. **Did the ambiguity rule handle simultaneous failures?** Not testable here with two known causes; on the one real
   simultaneous case it would not have fired.
5. **Did the scorer agree with the rule?** Yes, on all 6 named decisions, including while the second fault was
   present.
6. **Does real AWS evidence support enabling the ambiguity rule?** No evidence either way; the rule was never exercised.
7. **Enough evidence to activate the gate?** No: 6 named decisions, one failure type, no real wrong action it would
   have caught. The earlier gate bar stands on its own evidence; this session neither adds to it nor undoes it.

## What the 100% leaves out

- Only memory (a known cause with a real signal) and a database outage (escalation expected) were scored. The hard
  cases, overload, a crash loop, CPU starvation and a network block, did not run or did not take hold.
- Escalation is the correct answer for four of the six real failure types by construction, so a system that always
  escalated would score well on them. The memory trials are the only real test of naming a cause.
- Small, one session, one cluster, 200m limits, a 15-minute healthy window.

## Next

Rerun with the failure types that did not run, with a crash mechanism that works (delete the pod, or kill a
non-init process) and a way to fill the connection pool for real, then a longer healthy window.
