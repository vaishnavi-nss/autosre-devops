# Live validation, run 11

Third post-fix session. Commit c6cb6ef, scorer model `b565d77f` (retrained on runs 3, 4, 5-early and 8),
`SECOND_OPINION_MODE=shadow`. Harness started 07:54 UTC, finished 10:29 UTC (155 minutes), 14 trials
(3 per known cause, 5 with no known cause). Results are in
`s3://autosre-validation-841736140404/results-run11/`.

## What happened

- 14 trials ran. **3 were marked contaminated by the harness** (trials 2 `db_disk_full`, 3 and 7
  `high_memory_usage`) and are excluded from every count below, as `gate_check.py` does. They are not hidden: the
  log still shows them. 11 trials count.
- **No veto of a correct decision**, in the counted trials of any post-fix session.
- Unknown-cause trials: every one was escalated by the engine. The scorer disagreed with every wrong claim made on
  them except one `no_opinion` (an incident that arrived 41 s after injection, too early for a window).
- The one wrong claim the scorer did not catch across the three sessions is that `no_opinion` case.

## Gate check over runs 8, 9 and 11

`python evaluation/gate_check.py d8 d9 d11`

| criterion | met | measured |
|---|:---:|---|
| sessions with the fix >= 3 | yes | 3 |
| judged correct decisions >= 40 | **no** | 32 |
| first decisions of a trial >= 20 | **no** | 17 |
| vetoes of correct decisions <= 0 | yes | 0 of 32 (0 of 17 first decisions) |
| 95% upper bound on the veto rate <= 10% | **no** | 10.7% |
| true claims backed >= 95% of >= 40 probes | **no** | 34 of 34 |
| wrong claims caught >= 95% of >= 40 probes | yes | 45 of 46 |
| unknown-cause trials >= 5, all escalated | yes | 6 of 6 escalated |

**NOT READY. `SECOND_OPINION_MODE` stays `shadow`.**

## What is short, and why

The scorer did nothing wrong in this session; the shortfall is volume. Three contaminated trials removed about a
fifth of the judged decisions. Missing: 8 judged correct decisions (3 of them first decisions of a trial), and 6
true-claim probes. At 34 decisions with no veto the 95% upper bound is about 10.2%, at 40 about 8.8%, so the
bound will meet the 10% line once the count does.

A further session of about 3 trials per known cause (9 trials) would cover it if it stays clean. The bar was not
lowered to fit this result.

## Contamination

The harness sets `contaminated` on a trial when it cannot be sure the labelled fault is the only one acting. The
reason for trials 2, 3 and 7 was not read from the trial records when this was written (the log is in S3), so the
cause is unconfirmed. Two of the three are memory trials, so the settle time after memory faults is the first thing
to check before the next session.
