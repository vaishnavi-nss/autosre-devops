# Live validation results

Faults were **injected** into a running cluster (`CHAOS_ENABLED` endpoints); everything after that - the alert rules, Alertmanager grouping, the webhook, the engine - is the real pipeline. These measure detection and correlation; they do not show that real failures look like the injected ones.

Run 4 (2026-11-11): the second-opinion service loaded the **own-data model** (`second-opinion/models/own.joblib`, trained on run 3) in `shadow` mode.

- trials run: **14**; usable: **12**
- excluded (previous trial's alerts still firing, or the trial errored): #10, #12
- trials where an incident reached the engine: **12 of 12**

## Every trial

| # | injected | symptoms at first webhook | first decision | conf. | after | records | final decision | acts at 0.75 | outcome |
|---:|---|---|---|---:|---:|---:|---|---:|---|
| 1 | high_memory_usage | APILatencyHigh, ContainerMemoryHigh | high_memory_usage | 0.75 | 107s | 2 | escalate (0.00) | 107s | correct action (revised later) |
| 2 | high_memory_usage | APILatencyHigh | escalate | 0.00 | 50s | 3 | escalate (0.00) | 111s | missed |
| 3 | db_disk_full | DBDiskNearFull | db_disk_full | 0.60 | 95s | 1 | db_disk_full (0.60) | never | correct action |
| 4 | unknown | ErrorRateHigh | escalate | 0.00 | 133s | 3 | escalate (0.00) | never | correct escalation |
| 5 | db_disk_full | CPUThrottlingHigh, DBDiskNearFull | db_disk_full | 0.60 | 84s | 2 | escalate (0.00) | never | correct action (revised later) |
| 6 | db_connection_pool_exhaustion | CPUThrottlingHigh, DBPoolNearExhaustion | db_connection_pool_exhaustion | 0.55 | 69s | 4 | escalate (0.00) | 128s | correct action (revised later) |
| 7 | db_disk_full | DBDiskNearFull | db_disk_full | 0.60 | 95s | 3 | db_disk_full (0.60) | never | correct action |
| 8 | db_connection_pool_exhaustion | APILatencyHigh, CPUThrottlingHigh, DBPoolNearExhaustion | db_connection_pool_exhaustion | 0.65 | 93s | 5 | escalate (0.00) | 153s | correct action (revised later) |
| 9 | db_disk_full | APILatencyHigh, DBDiskNearFull, ErrorRateHigh | db_disk_full | 1.00 | 110s | 8 | escalate (0.00) | 110s | correct action (revised later) |
| 11 | high_memory_usage | APILatencyHigh, ContainerMemoryHigh | high_memory_usage | 0.75 | 107s | 3 | escalate (0.00) | 107s | correct action (revised later) |
| 13 | unknown | ErrorRateHigh | escalate | 0.00 | 154s | 3 | escalate (0.00) | never | correct escalation |
| 14 | high_memory_usage | APILatencyHigh, ContainerMemoryHigh | high_memory_usage | 0.75 | 108s | 3 | high_memory_usage (0.75) | 108s | correct action |

## Timing, known-cause trials

- first incident reaches the engine: median **95s** (range 50-110s) after injection
- first decision a dispatcher gated at 0.75 would act on: median **110s** (range 107-153s), in 7 of 10 trials; **never reached the gate** in trials #3, #5, #7
- the gap between the two is the cost of waiting for the consequences to fire: the deciding symptom alerts first and alone, so the first webhook usually scores about 0.55-0.60.

### First decision per trial

What the dispatcher would act on: the first webhook of each trial. REAL pipeline, INJECTED faults.

**12 incidents.** Accuracy: **91.7%**

| outcome | count | share |
|---|---:|---:|
| correct action | 9 | 75.0% |
| correct escalation | 2 | 16.7% |
| missed (escalated a known cause) | 1 | 8.3% |
| WRONG ACTION | 0 | 0.0% |

#### Confusion matrix

| true \ predicted | db connection pool exhaustion | db disk full | high memory usage | escalate |
|---|---:|---:|---:|---:|
| **db connection pool exhaustion** | 2 | 0 | 0 | 0 |
| **db disk full** | 0 | 4 | 0 | 0 |
| **high memory usage** | 0 | 0 | 3 | 1 |
| **escalate** | 0 | 0 | 0 | 2 |

#### Per class

| class | precision | recall | F1 | support |
|---|---:|---:|---:|---:|
| db_connection_pool_exhaustion | 100.0% | 100.0% | 100.0% | 2 |
| db_disk_full | 100.0% | 100.0% | 100.0% | 4 |
| high_memory_usage | 100.0% | 75.0% | 85.7% | 4 |
| escalate | 66.7% | 100.0% | 80.0% | 2 |

#### What a confidence gate buys

| min confidence | automated actions | wrong actions | precision of actions | known incidents auto-remediated |
|---:|---:|---:|---:|---:|
| 0.00 | 9 (75.0%) | 0 (0.0%) | 100.0% | 90.0% |
| 0.50 | 9 (75.0%) | 0 (0.0%) | 100.0% | 90.0% |
| 0.75 | 4 (33.3%) | 0 (0.0%) | 100.0% | 40.0% |
| 0.90 | 1 (8.3%) | 0 (0.0%) | 100.0% | 10.0% |

### Every record

All webhooks received, including later updates that carry more symptoms.

**40 incidents.** Accuracy: **65.0%** (correct action 20, correct escalation 6, missed 14, **WRONG ACTION 0**).

| true \ predicted | db connection pool exhaustion | db disk full | high memory usage | escalate |
|---|---:|---:|---:|---:|
| **db connection pool exhaustion** | 5 | 0 | 0 | 4 |
| **db disk full** | 0 | 8 | 0 | 6 |
| **high memory usage** | 0 | 0 | 7 | 4 |
| **escalate** | 0 | 0 | 0 | 6 |

## Second opinion (shadow mode: annotated, never acted on)

A separate service scores the raw metrics and says whether it agrees with the rules. REAL pipeline, INJECTED faults, model trained on this system's own earlier injected faults (run 3).

### The engine's own decisions

| # | injected | engine decision | rules were | scorer | why |
|---:|---|---|---|---|---|
| 1 | high_memory_usage | high_memory_usage | right | no_opinion | only 11 baseline samples before the incident |
| 3 | db_disk_full | db_disk_full | right | agree | metrics agree on database and on the fault type |
| 5 | db_disk_full | db_disk_full | right | disagree | metrics look like gateway_fault, not db_disk_full |
| 6 | db_connection_pool_exhaustion | db_connection_pool_exhaustion | right | disagree | metrics point at database, rules named transaction-service |
| 7 | db_disk_full | db_disk_full | right | agree | metrics agree on database and on the fault type |
| 8 | db_connection_pool_exhaustion | db_connection_pool_exhaustion | right | disagree | metrics look like gateway_fault, not db_connection_pool_exhaustion |
| 9 | db_disk_full | db_disk_full | right | agree | metrics agree on database and on the fault type |
| 11 | high_memory_usage | high_memory_usage | right | agree | metrics agree on transaction-service and on the fault type |
| 14 | high_memory_usage | high_memory_usage | right | disagree | metrics look like db_connection_pool_exhaustion, not high_memory_usage |

- on the **9** correct decisions the scorer said: agree 4, disagree 4, no_opinion 1. In gate mode, `disagree` would have withheld a correct action (**4 of 9**).

### Probed with a true and a deliberately wrong diagnosis

| # | injected | probed | claim: true diagnosis | claim: wrong diagnosis |
|---:|---|---|---|---|
| 1 | high_memory_usage | at first incident | no_opinion | no_opinion |
| 1 | high_memory_usage | end of hold | no_opinion | no_opinion |
| 2 | high_memory_usage | at first incident | agree | disagree |
| 2 | high_memory_usage | end of hold | agree | disagree |
| 3 | db_disk_full | at first incident | agree | disagree |
| 3 | db_disk_full | end of hold | agree | disagree |
| 4 | unknown | at first incident | - | disagree |
| 4 | unknown | end of hold | - | disagree |
| 5 | db_disk_full | at first incident | agree | disagree |
| 5 | db_disk_full | end of hold | agree | disagree |
| 6 | db_connection_pool_exhaustion | at first incident | agree | disagree |
| 6 | db_connection_pool_exhaustion | end of hold | agree | disagree |
| 7 | db_disk_full | at first incident | agree | disagree |
| 7 | db_disk_full | end of hold | agree | disagree |
| 8 | db_connection_pool_exhaustion | at first incident | agree | disagree |
| 8 | db_connection_pool_exhaustion | end of hold | agree | disagree |
| 9 | db_disk_full | at first incident | agree | disagree |
| 9 | db_disk_full | end of hold | agree | disagree |
| 11 | high_memory_usage | at first incident | agree | disagree |
| 11 | high_memory_usage | end of hold | agree | disagree |
| 13 | unknown | at first incident | - | disagree |
| 13 | unknown | end of hold | - | disagree |
| 14 | high_memory_usage | at first incident | disagree | disagree |
| 14 | high_memory_usage | end of hold | disagree | disagree |

- **true diagnosis** (20 probes): agree 16, disagree 2, no_opinion 2
- **wrong diagnosis** (24 probes): disagree 22, no_opinion 2. Caught (disagreed): **22 of 24**.
- a verifier is only useful if it agrees with the true claim and disagrees with the wrong one; `no_opinion` is safe but adds nothing, and `unavailable` means the call failed.
