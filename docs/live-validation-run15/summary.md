# Run 15 and the stuck-APILatencyHigh investigation

Why trials were lost to "contamination" (an `APILatencyHigh` that would not clear), what was measured on the live
cluster, and a short confirmation run. Session 2026-11-12, fresh lab, same instance, scorer `b565d77f`,
`SECOND_OPINION_MODE=shadow`. Results of the confirmation run: `s3://autosre-validation-841736140404/results-run15/`.

## Finding

**The healthy cluster's gateway p95 sits right on the alert's 0.5 s line, because the services are CPU-throttled by
their 200m limits.** `APILatencyHigh` fires on `histogram_quantile(0.95, ...[5m]) > 0.5`. With nothing injected, the
gateway's one-minute p95 was 0.45-0.77 s, so noise alone kept the alert firing, and `wait_quiet()` waited for something
that was not a leftover fault. It is a threshold-against-baseline problem, not a stuck alert and not a leftover fault.

## What was checked, in order

1. **CPU credits (ruled out).** The instance is `t3.large` in `unlimited` credit mode, so it is not throttled for
   credits. CloudWatch: `CPUCreditBalance` 0 throughout, surplus credits climbing steadily (up to 190 by the end of
   run 13, reset by the lab restart), node CPU 43-50% (49-53% late in run 13, about 44% in run 14). The node was not saturated.
2. **Prometheus history for the two stuck waits in run 14** (18:06-18:20 and 19:07-19:24 UTC, no fault injected;
   `correlation-engine/evaluation/diagnose_latency.py`): gateway p95 over one minute 0.45-0.77 s; transaction-service
   p95 0.25-0.44 s while the database's was about 0.00; frontend 0.7-0.9 s; 2-29% of gateway requests slower than 0.5 s;
   **22% of CPU periods throttled** in the `autosre` containers (their limits are 200m).
3. **Controlled change.** At 20:18 UTC the CPU limit of the four services was raised from 200m to 1000m on the
   instance (the repo manifests were not touched). Same idle cluster, steady state afterwards:

| one-minute measure | 200m limits | 1000m limits |
|---|---:|---:|
| gateway p95 | 0.45-0.77 s | 0.19-0.22 s |
| transaction-service p95 | 0.25-0.44 s | 0.07 s |
| frontend p95 | 0.7-0.9 s | 0.24 s |
| gateway requests over 0.5 s | 2-29% | 0% |
| gateway requests per second | 4-5 | about 7.4 |
| `APILatencyHigh` | firing | not firing |

The limits were restored to 200m afterwards, so the cluster matches the manifests again.

## Run 15: three trials with 1000m limits

`RUN=run15 PER_CAUSE=1 UNKNOWN=0 DEADLINE=30`, started 20:38 UTC, finished 21:00 UTC (22 minutes). The 1000m limit was
set by hand before the run, so the harness did not record it; the `--cpu-limit` flag added after this run does.

- **3 trials, 0 contaminated, no retry needed**, 18 "waiting for quiet" polls in the whole log. Each trial took
  6-9 minutes; in run 14 they took 12-14 minutes, and one memory trial 33.
- Scorer: disk and memory trials agreed with the true cause and disagreed with the wrong claim at the first incident
  and at the end of the hold. The pool trial was `no_opinion` for both claims at the first incident, then agreed and
  disagreed correctly at the end, the same pattern as run 14's pool trial.
- **Not pooled into the gate evidence.** Three trials is not a basis for a claim, and the four sessions the gate
  rests on (runs 11-14) ran with the 200m limits.

## What this does and does not show

- It explains the lost trials in runs 11-14 well: in the windows read, the alert was firing with no fault present, and
  removing the throttling removed it. Run 14's two stuck waits are the direct evidence; runs 11-13's Prometheus history
  is gone, so for those the explanation is consistent with the data (run 13 got worse as the session aged and its node
  CPU was a few points higher) but was not observed.
- Why a session gets worse with age is still not shown. A guess is more background load later (Prometheus churn from
  the repeated pod replacements) pushing throttling up; untested.
- **The scorer evidence was gathered under throttled services.** The scorer works on each service's movement from its
  own baseline, so it may well hold up, and run 15's three trials agree, but that is not shown. If the gate matters
  for a differently sized cluster, a session at the new limits is worth running before relying on the result.

## What to change

- **Cluster:** raise the services' CPU limits (the manifests in `k8s/*.yaml` say 200m with a comment that they are
  deliberately low). Those files already exist on the teammate's `main`, so this branch does not edit them; the
  harness now takes `--cpu-limit 1000m` instead. The memory limit, which the memory fault depends on, is unchanged.
- **Alert:** if the limits must stay low, the 0.5 s threshold is inside the healthy range and needs raising (or a
  longer `for:`); that is the cloud role's rule file.
