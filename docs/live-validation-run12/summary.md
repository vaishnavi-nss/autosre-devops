# Live validation, run 12

Fourth post-fix session, run to close the volume gap left by run 11. Commit 72d91ec, scorer model `b565d77f`,
`SECOND_OPINION_MODE=shadow`. Started 10:42 UTC, stopped at its 100-minute deadline at 12:24 UTC after **9 trials**
(planned 12: 4 per known cause, no unknown-cause trials because that criterion was already met). Results are in
`s3://autosre-validation-841736140404/results-run12/`.

## What happened

- 9 trials ran. **2 were marked contaminated** by the harness (trials 3 and 8, both `db_connection_pool_exhaustion`)
  and are excluded, as `gate_check.py` excludes them. 7 trials count. The reason was not read from the trial records.
- No veto of a correct decision in this session. At the first incident and at the end of the hold the scorer agreed
  with the true cause and disagreed with the wrong claim in every trial that was read live.
- The harness is slower than planned (about 11 minutes a trial, not 6), which is why the deadline cut it to 9.

## Gate check over runs 8, 9, 11 and 12

`python evaluation/gate_check.py d8 d9 d11 d12`

| criterion | met | measured |
|---|:---:|---|
| sessions with the fix >= 3 | yes | 4 |
| judged correct decisions >= 40 | yes | 43 |
| first decisions of a trial >= 20 | yes | 24 |
| vetoes of correct decisions <= 0 | yes | 0 of 43 (0 of 24 first decisions) |
| 95% upper bound on the veto rate <= 10% | yes | 8.2% |
| true claims backed >= 95% of >= 40 probes | yes | 48 of 48 |
| wrong claims caught >= 95% of >= 40 probes | yes | 59 of 60 |
| unknown-cause trials >= 5, all escalated | yes | 6 of 6 escalated |

**READY as the bar is written.** `SECOND_OPINION_MODE` is still `shadow`; the bar says the scorer is ready to gate,
and turning it on is a separate decision.

## What the READY result rests on

Read this before acting on it.

1. **It pools two scorer versions.** Run 8's verdicts were made by the scorer that was deployed before it was
   retrained on run 8. Runs 11 and 12 used the retrained model `b565d77f`. The summary of run 9 does not record
   which model made its verdicts, so it is unverified.
2. **The current model's own evidence is thinner.** Runs 11 and 12 alone (the two sessions known to use
   `b565d77f`): 22 judged decisions (13 first decisions), 0 vetoes, 95% upper bound **14.9%**, 26 of 26 true claims
   backed, 35 of 36 wrong claims caught. That does not meet the bar on its own (2 sessions, 22 decisions).
3. **The one miss** across all sessions is a `no_opinion` on a wrong claim made on an incident that arrived 41 s
   after injection, too early for a window. It is not a false endorsement.
4. **Five contaminated trials** across runs 11 and 12 were excluded (run 11 trials 2, 3, 7; run 12 trials 3, 8).
   That is a lot, and the cause has not been found.
5. **Fault coverage.** The trials cover four causes on a small cluster with synthetic faults. The bar measures
   whether the scorer vetoes correct diagnoses in that setting, not in production.

## Recommendation

Do not switch to gate mode on this alone. Either (a) accept the pooled result, knowing the caveats above, or
(b) collect evidence with `b565d77f` only: it has 22 judged decisions and the bar needs 40, so about 18 more, which
is roughly two more sessions of this size. That would settle points 1 and 2. Either way
the first step in production should stay shadow, with the gate switched on for one cause at a time.
