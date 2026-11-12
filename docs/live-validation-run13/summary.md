# Live validation, run 13

Fifth post-fix session and the first run on the retrained scorer `b565d77f` alone, to build evidence for that model
without pooling in older versions. Commit e9d588f, `SECOND_OPINION_MODE=shadow`. Harness started 13:43 UTC and stopped
at its 190-minute deadline at 16:55 UTC after **16 trials** (6 per cause planned; no unknown-cause trials). Results are
in `s3://autosre-validation-841736140404/results-run13/`.

## What happened

- 16 trials ran. **9 were marked contaminated** and are excluded, as `gate_check.py` excludes them: trials 2, 13, 14,
  16 (`high_memory_usage`), 5, 9, 11 (`db_disk_full`) and 8, 10 (`db_connection_pool_exhaustion`). **7 trials count**:
  1, 3, 4, 6, 7, 12 and 15 (from `trials.jsonl`).
- Every contaminated trial was held up by `APILatencyHigh` that did not clear within the quiet limit, often together
  with `ErrorRateHigh` or a database alert. The fault is not left over: faults live in pod memory and the pods are
  replaced between trials. The rate rose as the session got older (1 of the first 4 trials, 8 of the last 12). The
  likely cause is a cluster that gets slower late in a long session; **this is a hypothesis and was not confirmed**.
- No veto of a correct decision. Every probe miss was a `no_opinion`, never a false endorsement and never a
  disagreement with a true claim.

## Gate check on the retrained model only (runs 11, 12, 13)

`python evaluation/gate_check.py d11 d12 d13`

| criterion | met | measured |
|---|:---:|---|
| sessions with the fix >= 3 | yes | 3 |
| judged correct decisions >= 40 | **no** | 34 |
| first decisions of a trial >= 20 | yes | 20 |
| vetoes of correct decisions <= 0 | yes | 0 of 34 (0 of 20 first decisions) |
| 95% upper bound on the veto rate <= 10% | **no** | 10.2% |
| true claims backed >= 95% of >= 40 probes | yes | 39 of 40 |
| wrong claims caught >= 95% of >= 40 probes | yes | 48 of 50 |
| unknown-cause trials >= 5, all escalated | yes | 5 of 5 escalated |

**NOT READY on the retrained model alone.** `SECOND_OPINION_MODE` stays `shadow`.

## What is short

Six judged correct decisions. At 34 decisions with no veto the 95% upper bound is 10.2%; at 40 it is about 8.8%, so
the bound meets its line once the count does. The scorer did nothing wrong; the shortfall is volume lost to
contamination.

## Harness change made after this run

`live_validate.py` now (a) waits up to 900 s for quiet, (b) if the cluster is still not quiet it replaces the pods once
more and waits again before marking the trial contaminated, and (c) records which alerts were still firing in
`still_firing` on the trial. None of that was active in this run. Whether it reduces contamination is untested until
run 14.
