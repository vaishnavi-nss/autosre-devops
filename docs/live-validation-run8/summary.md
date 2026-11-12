# Run 8: live shadow test with the fault-start fix

2026-11-12, same AWS Academy Learner Lab instance as run 7, `SECOND_OPINION_MODE=shadow`, scorer =
`second-opinion/models/own.joblib` (unchanged), engine and scorer rebuilt from the branch with the fix
(`estimate_fault_start`: the start comes from the deciding alert minus its own `for:` delay, sent as
`fault_start`). The pod printed `new code: True` before the harness started. All pods in `autosre` and
`monitoring` were restarted first, so **no stuck `APILatencyHigh` from an earlier trial was firing** (it was in
run 7). Started 00:34 UTC; planned 7 trials (2 per cause, 1 unknown).

`report.md` was not read: the figures below come from `run8.log` and `incidents.jsonl` (141 KB, in the
bucket), read at 01:38 UTC. The seventh trial (unknown, gateway latency) had injected and the engine had
received its incident, but its records and final verdict are **not** included here.

## Probes: the true claim and a deliberately wrong one

All 6 known-cause trials, at the first incident and at the end of the hold (12 probe moments):
true claim **agree 12 of 12**, wrong claim **disagree 12 of 12** (same as run 7).

## The engine's own decisions

Every webhook record that carried a diagnosis (11, all the correct cause) and the scorer's verdict on it, with
the estimated fault start minus the real injection time:

| trial | injected | records (verdicts) | estimate − injection |
|---:|---|---|---:|
| 1 | db_disk_full | agree, agree | -7 s |
| 2 | db_disk_full | agree, agree, agree | -10 s |
| 3 | db_connection_pool_exhaustion | agree, agree, agree | +5 s |
| 4 | high_memory_usage | agree (one diagnosed record) | +16 s |
| 5 | high_memory_usage | agree (one diagnosed record) | +24 s |
| 6 | db_connection_pool_exhaustion | agree | +4 s |

**11 of 11 agree; 0 disagree, 0 `no_opinion`.** Run 7 on the same measure: agree 5, disagree 5, no_opinion 3.
The first decision of each trial was backed in 6 of 6 (run 7: 2 of 6, with 3 vetoed).

The remaining records in the file carried no diagnosis (only `APILatencyHigh` had fired yet, or a later update
after the fault cleared) and were not scored: the engine escalated them, as it did in earlier runs. In trial 4
the first webhook was one of these, and the diagnosis came 60 s later.

## What this does and does not show

- The estimate lands within about ±25 s of the real start on every scored record, against -788 s to +78 s with the
  old method on run 7.
- **It cannot separate the fix from the cleaner environment.** Run 8 had no stale alert, and on a clean run
  the old method would also have started the window about 70 s after the fault, which the model handles. So run 8
  shows that with the fix and a clean cluster the scorer agrees with every correct decision. It does not show the
  fix is what removed the vetoes; that rests on the run 7 replay (start times only) and on the mechanism found
  there (every vetoed record had a start before the fault).
- To test the fix alone, rerun with an alert left firing from before the fault (for example, skip the quiet wait), and
  compare the old and new start. Not done.
- 6 usable trials, 11 scored decisions: small. No unknown-cause trial completed here, so escalation is untested in
  this run.
