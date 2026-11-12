# Run 7: live shadow test of the retrained own-data scorer

2026-11-11, AWS Academy Learner Lab, one `t3.large` node, `SECOND_OPINION_MODE=shadow`, scorer =
`second-opinion/models/own.joblib` (47 trials, 115 windows: runs 3, 4 and 5-early). Started 18:40 UTC,
stopped at its 80 minute deadline after **7 of 11 planned trials** (84 minutes): a stuck `APILatencyHigh`
alert kept each trial waiting 10-25 minutes for quiet.

`report.md` was not produced (the harness did not write one before it stopped); the figures below come
from `run7.log` and `incidents.jsonl` (164 KB, in the bucket, not copied here).

## Trials

| # | injected | incident reached the engine after | note |
|---:|---|---:|---|
| 1 | high_memory_usage | 102 s | scorer `no_opinion`: only 1 baseline sample (pods had just been restarted) |
| 2 | unknown (gateway latency 900 ms) | never | **excluded**: no incident before the 7 minute timeout |
| 3 | db_disk_full | 134 s | |
| 4 | db_disk_full | 71 s | |
| 5 | db_connection_pool_exhaustion | 92 s | |
| 6 | high_memory_usage | 71 s | |
| 7 | high_memory_usage | 82 s | |

Usable: 6 (trial 2 excluded; no `unknown` trial completed, so escalation was not tested in this run).
Run 6, the first attempt, was invalid: the lab restart rebooted the instance mid-run.

## Probes: the true claim and a deliberately wrong one

At the first incident and at the end of the hold, in all 6 usable trials (12 probe moments):

- true diagnosis: **agree 12 of 12**
- wrong diagnosis: **disagree 12 of 12**

## The engine's own decisions (what a gate would have acted on)

Every engine decision was the correct cause. Scorer verdicts on them, one per webhook record:

| trial | injected | verdicts in order |
|---:|---|---|
| 1 | high_memory_usage | no_opinion, no_opinion, no_opinion |
| 3 | db_disk_full | agree |
| 4 | db_disk_full | **disagree** ("metrics point at transaction-service"), agree |
| 5 | db_connection_pool_exhaustion | **disagree**, **disagree**, **disagree** (all "metrics look like gateway_fault") |
| 6 | high_memory_usage | **disagree** ("metrics point at api-gateway"), agree |
| 7 | high_memory_usage | agree, agree |

- all 13 decisions: agree 5, disagree 5, no_opinion 3
- **first decision of each trial** (the one a dispatcher acts on): agree 2, disagree 3, no_opinion 1
- the 3 of 6 first decisions that would have been vetoed were all correct actions

## Why the engine path vetoes (checked afterwards from `incidents.jsonl`)

The incident start the engine passes is the earliest `startsAt` of all firing alerts, and a stale
`APILatencyHigh` from the previous trial put it 98-788 s before the injection in 7 of 13 records. All
records with a start 69-78 s after injection were backed (5); all with a start before the fault or only 4 s
after it were vetoed (5); the 3 `no_opinion` records are trial 1, which also had too few baseline samples.
Details and the table are in `docs/evaluation.md`.

## Reading it

Same pattern as run 4, with the same cause: the scorer is right when asked by the harness
(`injection + 60 s`) and vetoes about half the correct decisions when asked by the engine
(`startsAt` of the earliest alert). In trial 5 both calls looked at the same incident, one said agree for
the true claim and the other disagreed with the rules' identical claim. The model is therefore unlikely to
be the weak part; the start time the engine passes is (see above). Six trials is a small sample.
