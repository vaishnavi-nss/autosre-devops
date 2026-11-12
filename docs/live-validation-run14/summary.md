# Live validation, run 14

Sixth post-fix session, run on a freshly started lab to top up the retrained scorer `b565d77f` after run 13 fell short.
Commit bd3c797 (the harness with the contamination retry), `SECOND_OPINION_MODE=shadow`. Harness started 17:52 UTC and
stopped at its 120-minute deadline at 19:53 UTC after **7 trials** (3 per cause planned, no unknown-cause trials because
that criterion was already met). Results are in `s3://autosre-validation-841736140404/results-run14/`.

## What happened

- 7 trials ran: 3 `db_disk_full`, 3 `high_memory_usage`, 1 `db_connection_pool_exhaustion` (the plan was 9; the deadline
  cut it). **1 was marked contaminated** (trial 2, `high_memory_usage`) and is excluded as `gate_check.py` excludes it.
  **6 trials count.**
- The retry added after run 13 ran twice. In trial 2 `APILatencyHigh` was still firing after the retry, so the trial
  stayed contaminated. In trial 5 `APILatencyHigh` was firing before the retry and the trial counted afterwards.
- Contamination was 1 of 7 here against 9 of 16 in run 13. That is a better number, but the two sessions differ in more
  than the retry (this one ran on a freshly started lab, run 13 late in a long one) and the sample is small, so **this
  does not show that the retry is what fixed it.** Whether the retry or session age matters is still untested.
- No veto of a correct decision. Where the scorer had an opinion it agreed with the true cause and disagreed with the
  wrong claim in every counted trial, at the first incident and at the end of the hold.
- Both probe misses were `no_opinion` on trial 6 (`db_connection_pool_exhaustion`) at the first incident, one on the
  true claim and one on the wrong claim. Neither is a false endorsement. At the end of that hold the scorer agreed with
  the true cause and disagreed with the wrong claim.

## Gate check on the retrained model only (runs 11, 12, 13, 14)

`python evaluation/gate_check.py d11 d12 d13 d14`

| criterion | met | measured |
|---|:---:|---|
| sessions with the fix >= 3 | yes | 4 |
| judged correct decisions >= 40 | yes | 46 |
| first decisions of a trial >= 20 | yes | 26 |
| vetoes of correct decisions <= 0 | yes | 0 of 46 (0 of 26 first decisions) |
| 95% upper bound on the veto rate <= 10% | yes | 7.7% |
| true claims backed >= 95% of >= 40 probes | yes | 50 of 52 |
| wrong claims caught >= 95% of >= 40 probes | yes | 59 of 62 |
| unknown-cause trials >= 5, all escalated | yes | 5 of 5 escalated |

**READY, on the retrained model's own evidence, with no older scorer version pooled in.** `SECOND_OPINION_MODE` is
still `shadow`; the bar says the scorer is ready to gate, and turning it on is a separate decision.

## What the READY result rests on

1. **It meets the bar as written, not by a wide margin.** The veto-rate bound is 7.7% against a 10% line, and 46
   decisions against 40.
2. **Every miss is a `no_opinion`.** Across runs 11 to 14 the scorer never endorsed a wrong claim and never disagreed
   with a true one. The misses cluster at the first incident, before a window exists; by the end of each hold it
   had an opinion. So gating should treat `no_opinion` as "do not veto", which is what shadow mode already models.
3. **The unknown-cause evidence is from run 11 only** (5 of 5 escalated). Runs 12 to 14 ran none.
4. **Contamination remains the weak point of the harness.** Counted trials were 11 of 14 (run 11), 7 of 9 (run 12),
   7 of 16 (run 13) and 6 of 7 (run 14): 31 of 46 overall, so 15 trials of lab time produced nothing. The cause is
   still unconfirmed.
5. **Fault coverage.** Four causes, a small cluster, synthetic faults. The bar measures whether the scorer vetoes
   correct diagnoses in that setting, not in production.

## Recommendation

Accepting this is now a judgement call about risk, not about volume. If you switch on gate mode, do it one cause at a
time with shadow logging kept on, and keep treating `no_opinion` as a pass-through.
