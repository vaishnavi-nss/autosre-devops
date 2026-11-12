# Run 9: the stale-alert test (partial: 3 of 6 trials read)

2026-11-12, same instance as runs 7 and 8, `--stale` mode of `live_validate.py`, engine and scorer with the
fault-start fix. Each trial first turns on gateway latency so `APILatencyHigh` fires (about 91 s later) and lets it
age 120 s, then injects the real fault on top. Every incident is then put to the scorer twice, about the same
incident: with the **old** start (earliest `startsAt` of every firing alert) and the **new** one (the deciding alert
wound back by its `for:`), and the new start is also asked about a deliberately wrong diagnosis.

**Read at 02:55 UTC from `run9.log`, three of six trials; the other three were still running or unread.** The raw
files (`incidents.jsonl`, `trials.jsonl`) are in the bucket under `results-run9/` and are not copied here. These
figures come from the log lines, not from the report script.

| trial | injected | old start vs real fault | old method | new start vs real fault | new method | new, wrong claim | engine's own verdict |
|---:|---|---:|---|---:|---|---|---|
| 1 | db_disk_full | -132 s | disagree, disagree | +3 s | agree, agree | disagree, disagree | agree |
| 2 | db_disk_full | -129 s | disagree, **agree** | -9 s | agree, agree | disagree, disagree | agree |
| 3 | db_connection_pool_exhaustion | -123 s | disagree, disagree | +12 s | agree, agree | disagree, disagree | agree |

(two probe moments per trial: at the first diagnosis and at the end of the hold)

- **Old method: disagreed with the true cause in 5 of 6 probes**, each with a start 123-132 s before the fault. The
  one agree (trial 2, end of hold) is the same incident one probe later.
- **New method: agreed in 6 of 6**, with the estimated fault start within -9 s to +12 s of the real one, and
  **disagreed with the wrong claim in 6 of 6**.
- The engine, which runs the new code, agreed with the correct diagnosis in all 3 trials.

So with a stale alert deliberately left firing, the old start vetoes the correct diagnosis and the new one does
not. That is the controlled comparison run 8 could not give. Three trials is a small sample; it is a consistent
pattern with the run 7 finding, not a proof. Trials 4-6 (memory, memory, pool) and the rest of the figures are
not read here.
