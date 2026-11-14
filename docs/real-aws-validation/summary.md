# Real AWS end-to-end validation (two sessions)

The RCA pipeline tested against real failures of the lab's own resources, with no chaos endpoint and no event sent to
the engine. Sessions 2026-11-13, lab instance `i-04f8f4d75378a1aba`, scorer `b565d77f`, **as deployed**: gate in
`shadow`, `ESCALATE_ON_AMBIGUITY` off, services at the manifests' 200m CPU limits. Raw results:
`s3://autosre-validation-841736140404/results-real1/` to `results-real7/`. Driver: `real_aws_validate.py`; numbers from
`real_aws_report.py` (scoring rules written at the top of that file before any data existed).

Each fault is an ordinary action on a real resource: a memory-holding process inside the transaction-service container,
scaling the database to zero, SIGTERM to PID 1 every 30 s, busy loops in the gateway container, a NetworkPolicy denying
traffic to the database. The engine is never told what was caused; the truth is only in the trial record.

## Headline

**REAL AWS END-TO-END ACCURACY = 100% (9 of 9 scored single-fault trials), 95% Wilson interval about 70% to 100%.**

How X was computed: scored single-fault trials in which the rule engine's *first* diagnosis was right (it named the
true cause for a known-cause failure, or escalated for a failure with no known cause), divided by all scored
single-fault trials. A trial is scored only if its baseline was healthy and the real failure was measured as effective
(a real metric crossed a threshold). 4 of 13 single-fault trials were not scored because the failure never took hold.
Two-fault trials and healthy windows are not in the number.

**This is not a claim of 100% accuracy.** Nine trials cannot support one, and 6 of the 9 are escalation-is-correct
cases (see "What the 100% leaves out").

| Failure type | Trials | Correct | Missed | Wrong | Escalated | Accuracy |
|---|---:|---:|---:|---:|---:|---:|
| memory_pressure (known cause) | 3 | 3 | 0 | 0 | 0 | 100% (3/3) |
| db_outage (no known cause) | 2 | 2 | 0 | 0 | 2 | 100% (2/2) |
| network_partition (no known cause) | 2 | 2 | 0 | 0 | 2 | 100% (2/2) |
| cpu_pressure (no known cause) | 1 | 1 | 0 | 0 | 1 | 100% (1/1) |
| pod_crash (no known cause) | 1 | 1 | 0 | 0 | 1 | 100% (1/1) |
| pool_exhaustion | 3 runs | not scored: the real failure never took hold (peak 0.65, 0.0, 0.0 of 0.9 needed) | | | | |
| pod_crash, first attempt (SIGKILL) | 1 run | not scored: no restarts | | | | |

| Metric | Real AWS result |
|---|---:|
| End-to-end accuracy (first decision) | 100% (9/9) |
| Correct diagnoses / correct escalations | 9 |
| Misses (no webhook reached the engine) | 0 of 9 |
| Wrong diagnoses | 0 of 9 |
| Wrong actions | 0 of 9 (no dispatcher is deployed; "action" is the playbook the engine would hand over) |
| Escalations (first decision) | 6 of 9 (all correct) |
| Gate disagreements (shadow) | 0 of 12 named decisions |
| Ambiguity escalations the rule would make | 0 |
| Successful recoveries (quiet again within the wait) | 11 of 16 |
| Precision / recall / F1 | 1.00 for both classes (`high_memory_usage` support 3, `none` support 6) |
| Median detection / decision latency | 115 s (n=9) / 107 s (n=9) |
| Median recovery latency | 296 s (n=11) |

## Healthy period (no fault)

15 minutes with nothing done: **0 false positives, 0 false escalations, 0 false actions, no alert fired.** A single
window; not enough to say the false-positive rate is zero. The second session's longer healthy window did not run: the
lab time went on slow recoveries.

## Two real faults at once (3 trials, all with both faults effective)

| Pair | Named | Playbook | Webhooks | Live escalations |
|---|---|---|---:|---:|
| memory + db_outage | high_memory_usage only | scale_replicas.yml | 5 | 2 |
| memory + network_partition (x2) | high_memory_usage only | scale_replicas.yml | 6 and 8 | 3 and 5 |

- In every pair the engine named the one cause it has a signature for (memory) while the other, real fault was never
  named; later webhooks escalated once the other fault's alerts arrived. The scorer agreed with memory every time.
- **The ambiguity rule never had its trigger.** It needs two *known* causes' deciding symptoms at once, and each pair
  had one known cause. 0 ambiguity escalations would have been made. A real two-known-cause test was **not possible**:
  the disk signal is a fixed value and the pool could not be filled for real.

## What did not work, exactly

1. **`pool_exhaustion`:** three designs. CPU cut on the database was rejected by Kubernetes, then left the database pod
   unready; 100 loops through the frontend peaked at 0.65 of the pool; 150 parallel clients straight at
   transaction-service peaked at 0.0. Real client load alone does not fill the pool here. Not scored.
2. **`pod_crash`:** SIGKILL to PID 1 is ignored; SIGTERM works and the second attempt was effective and scored.
3. **Disk and stopping the EC2 instance:** not testable end to end: the disk metric is a fixed value, and the instance
   hosts the monitoring being tested.
4. **Recovery:** 5 of 16 trials did not return to quiet within 15 minutes of stopping the fault (the alerts were still
   firing, e.g. `APILatencyHigh` at the 200m limit). This slowed every later trial and cut the second session short:
   one cpu_pressure trial, a second pod_crash, memory plus db_outage, a third memory trial and the healthy window did
   not run. The harness was stopped by me at the end; pods were found running, no leftover load pod.

## Answers

1. **How well did it diagnose real AWS failures?** Correctly on all 9 scored trials across five failure types; but
   only memory tests naming a cause, and that is 3 trials.
2. **Wrong actions?** None among the scored trials. In all 3 pair trials it acted (`scale_replicas.yml`) on memory
   while another real fault was in progress; that recommendation did not address the other fault.
3. **Missed?** None of the scored trials. (Failures that never took hold are not misses.)
4. **Did the ambiguity rule handle simultaneous failures?** Not tested: no real pair had two known causes. On the
   3 real pairs it would not have fired.
5. **Did the scorer agree with the rule?** Yes, 12 of 12 named decisions, including while the second fault was present.
6. **Does real AWS evidence support enabling the ambiguity rule?** No evidence either way. It was never exercised.
7. **Enough evidence to activate the gate?** No: 12 named decisions, one named cause, no disagreement and no real wrong
   action for a veto to catch. The earlier gate bar stands on its own evidence.

## What the 100% leaves out

- Six of the nine scored trials are failures with no known cause, where escalating is correct by construction; a
  system that always escalated would score 6/6 on them. Only the 3 memory trials test naming a cause.
- Overload (pool exhaustion) and disk, the other two known causes, could not be produced for real.
- Small, one cluster, 200m limits, one 15-minute healthy window, 5 of 16 recoveries incomplete.
