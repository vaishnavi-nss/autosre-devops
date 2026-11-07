# Live validation results

Faults were **injected** into a running cluster (`CHAOS_ENABLED` endpoints); everything after that - the alert rules, Alertmanager grouping, the webhook, the engine - is the real pipeline. These measure detection and correlation; they do not show that real failures look like the injected ones.

- trials run: **15**; usable: **15**
- trials where an incident reached the engine: **15 of 15**

## Every trial

| # | injected | symptoms at first webhook | first decision | conf. | after | records | final decision | acts at 0.75 | outcome |
|---:|---|---|---|---:|---:|---:|---|---:|---|
| 1 | high_memory_usage | APILatencyHigh, ContainerMemoryHigh | high_memory_usage | 0.75 | 103s | 2 | escalate (0.00) | 103s | correct action (revised later) |
| 2 | high_memory_usage | APILatencyHigh, ContainerMemoryHigh | high_memory_usage | 0.75 | 134s | 2 | escalate (0.00) | 134s | correct action (revised later) |
| 3 | db_disk_full | DBDiskNearFull | db_disk_full | 0.60 | 135s | 3 | db_disk_full (0.60) | never | correct action |
| 4 | unknown | ErrorRateHigh | escalate | 0.00 | 132s | 3 | escalate (0.00) | never | correct escalation |
| 5 | db_disk_full | CPUThrottlingHigh, DBDiskNearFull | db_disk_full | 0.60 | 85s | 2 | db_disk_full (0.60) | never | correct action |
| 6 | db_connection_pool_exhaustion | DBPoolNearExhaustion | db_connection_pool_exhaustion | 0.55 | 74s | 4 | escalate (0.00) | 134s | correct action (revised later) |
| 7 | db_disk_full | DBDiskNearFull | db_disk_full | 0.60 | 137s | 1 | db_disk_full (0.60) | never | correct action |
| 8 | db_connection_pool_exhaustion | DBPoolNearExhaustion | db_connection_pool_exhaustion | 0.55 | 73s | 5 | escalate (0.00) | 133s | correct action (revised later) |
| 9 | db_disk_full | APILatencyHigh, DBDiskNearFull, ErrorRateHigh | db_disk_full | 1.00 | 136s | 4 | escalate (0.00) | 136s | correct action (revised later) |
| 10 | db_connection_pool_exhaustion | DBPoolNearExhaustion | db_connection_pool_exhaustion | 0.55 | 75s | 6 | escalate (0.00) | 135s | correct action (revised later) |
| 11 | high_memory_usage | APILatencyHigh, ContainerMemoryHigh | high_memory_usage | 0.75 | 118s | 3 | escalate (0.00) | 118s | correct action (revised later) |
| 12 | high_memory_usage | APILatencyHigh, ContainerMemoryHigh | high_memory_usage | 0.75 | 134s | 2 | escalate (0.00) | 134s | correct action (revised later) |
| 13 | unknown | ErrorRateHigh | escalate | 0.00 | 135s | 3 | escalate (0.00) | never | correct escalation |
| 14 | high_memory_usage | APILatencyHigh, ContainerMemoryHigh | high_memory_usage | 0.75 | 132s | 2 | escalate (0.00) | 132s | correct action (revised later) |
| 15 | db_disk_full | DBDiskNearFull | db_disk_full | 0.60 | 122s | 3 | db_disk_full (0.60) | never | correct action |

## Timing, known-cause trials

- first incident reaches the engine: median **122s** (range 73-137s) after injection
- first decision a dispatcher gated at 0.75 would act on: median **134s** (range 103-136s), in 9 of 13 trials; **never reached the gate** in trials #3, #5, #7, #15
- the gap between the two is the cost of waiting for the consequences to fire: the deciding symptom alerts first and alone, so the first webhook usually scores about 0.55-0.60.

### First decision per trial

What the dispatcher would act on: the first webhook of each trial. REAL pipeline, INJECTED faults.

**15 incidents.** Accuracy: **100.0%**

#### Outcomes

| outcome | count | share |
|---|---:|---:|
| correct action | 13 | 86.7% |
| correct escalation | 2 | 13.3% |
| missed (escalated a known cause) | 0 | 0.0% |
| WRONG ACTION | 0 | 0.0% |

#### Confusion matrix

| true \ predicted | db connection pool exhaustion | db disk full | high memory usage | escalate |
|---|---:|---:|---:|---:|
| **db connection pool exhaustion** | 3 | 0 | 0 | 0 |
| **db disk full** | 0 | 5 | 0 | 0 |
| **high memory usage** | 0 | 0 | 5 | 0 |
| **escalate** | 0 | 0 | 0 | 2 |

#### What a confidence gate buys

An action is taken only if a cause was named *and* confidence >= the gate; everything else escalates to a human.

| min confidence | automated actions | wrong actions | precision of actions | known incidents auto-remediated |
|---:|---:|---:|---:|---:|
| 0.00 | 13 (86.7%) | 0 (0.0%) | 100.0% | 100.0% |
| 0.50 | 13 (86.7%) | 0 (0.0%) | 100.0% | 100.0% |
| 0.75 | 6 (40.0%) | 0 (0.0%) | 100.0% | 46.2% |
| 0.90 | 1 (6.7%) | 0 (0.0%) | 100.0% | 7.7% |

### Every record

All webhooks received, including later updates that carry more symptoms. 45 incidents; the
62.2% "accuracy" is an artefact of lingering consequence alerts being escalated after the cause
cleared (0 wrong actions; 17 "missed" are those later updates). See docs/evaluation.md.

## Second opinion (shadow mode: annotated, never acted on)

A separate service scores the raw metrics and says whether it agrees with the rules. REAL pipeline, INJECTED faults, a model trained on a different dataset.

### The engine's own decisions

| # | injected | engine decision | rules were | scorer | why |
|---:|---|---|---|---|---|
| 1 | high_memory_usage | high_memory_usage | right | disagree | metrics look like loss, not mem |
| 2 | high_memory_usage | high_memory_usage | right | disagree | metrics point at frontend, rules named transaction-service |
| 3 | db_disk_full | db_disk_full | right | no_opinion | scorer confidence 0.29 is below 0.30 |
| 5 | db_disk_full | db_disk_full | right | disagree | metrics point at frontend, rules named database |
| 6 | db_connection_pool_exhaustion | db_connection_pool_exhaustion | right | agree | metrics also point at transaction-service; no matching fault type to compare |
| 7 | db_disk_full | db_disk_full | right | disagree | metrics point at api-gateway, rules named database |
| 8 | db_connection_pool_exhaustion | db_connection_pool_exhaustion | right | disagree | metrics point at api-gateway, rules named transaction-service |
| 9 | db_disk_full | db_disk_full | right | agree | metrics also point at database; no matching fault type to compare |
| 10 | db_connection_pool_exhaustion | db_connection_pool_exhaustion | right | disagree | metrics point at api-gateway, rules named transaction-service |
| 11 | high_memory_usage | high_memory_usage | right | no_opinion | scorer confidence 0.27 is below 0.30 |
| 12 | high_memory_usage | high_memory_usage | right | disagree | metrics look like loss, not mem |
| 14 | high_memory_usage | high_memory_usage | right | disagree | metrics look like loss, not mem |
| 15 | db_disk_full | db_disk_full | right | agree | metrics also point at database; no matching fault type to compare |

- on the **13** correct decisions the scorer said: agree 3, disagree 8, no_opinion 2. In gate mode, `disagree` would have withheld a correct action (**8 of 13**).

### Probed with a true and a deliberately wrong diagnosis

| # | injected | probed | claim: true diagnosis | claim: wrong diagnosis |
|---:|---|---|---|---|
| 1 | high_memory_usage | at first incident | disagree | disagree |
| 1 | high_memory_usage | end of hold | disagree | disagree |
| 2 | high_memory_usage | at first incident | disagree | disagree |
| 2 | high_memory_usage | end of hold | disagree | disagree |
| 3 | db_disk_full | at first incident | no_opinion | no_opinion |
| 3 | db_disk_full | end of hold | disagree | disagree |
| 4 | unknown | at first incident | - | disagree |
| 4 | unknown | end of hold | - | no_opinion |
| 5 | db_disk_full | at first incident | disagree | disagree |
| 5 | db_disk_full | end of hold | disagree | disagree |
| 6 | db_connection_pool_exhaustion | at first incident | agree | disagree |
| 6 | db_connection_pool_exhaustion | end of hold | agree | disagree |
| 7 | db_disk_full | at first incident | disagree | disagree |
| 7 | db_disk_full | end of hold | agree | disagree |
| 8 | db_connection_pool_exhaustion | at first incident | disagree | disagree |
| 8 | db_connection_pool_exhaustion | end of hold | disagree | disagree |
| 9 | db_disk_full | at first incident | agree | disagree |
| 9 | db_disk_full | end of hold | agree | disagree |
| 10 | db_connection_pool_exhaustion | at first incident | disagree | disagree |
| 10 | db_connection_pool_exhaustion | end of hold | agree | disagree |
| 11 | high_memory_usage | at first incident | no_opinion | no_opinion |
| 11 | high_memory_usage | end of hold | agree | disagree |
| 12 | high_memory_usage | at first incident | disagree | disagree |
| 12 | high_memory_usage | end of hold | disagree | disagree |
| 13 | unknown | at first incident | - | disagree |
| 13 | unknown | end of hold | - | disagree |
| 14 | high_memory_usage | at first incident | disagree | disagree |
| 14 | high_memory_usage | end of hold | disagree | disagree |
| 15 | db_disk_full | at first incident | agree | disagree |
| 15 | db_disk_full | end of hold | agree | disagree |

- **true diagnosis** (26 probes): agree 9, disagree 15, no_opinion 2
- **wrong diagnosis** (30 probes): disagree 27, no_opinion 3. Caught (disagreed): **27 of 30**.
- a verifier is only useful if it agrees with the true claim and disagrees with the wrong one; `no_opinion` is safe but adds nothing, and `unavailable` means the call failed.
