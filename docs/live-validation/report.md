# Live validation results

Faults were **injected** into a running cluster (`CHAOS_ENABLED` endpoints); everything after that - the alert rules, Alertmanager grouping, the webhook, the engine - is the real pipeline. These measure detection and correlation; they do not show that real failures look like the injected ones.

- trials run: **18**; usable: **17**
- excluded (previous trial's alerts still firing, or the trial errored): #3
- trials where an incident reached the engine: **17 of 17**

## Every trial

| # | injected | symptoms at first webhook | first decision | conf. | after | records | final decision | acts at 0.75 | outcome |
|---:|---|---|---|---:|---:|---:|---|---:|---|
| 1 | unknown | ErrorRateHigh | escalate | 0.00 | 115s | 3 | escalate (0.00) | never | correct escalation |
| 2 | db_connection_pool_exhaustion | CPUThrottlingHigh, DBPoolNearExhaustion | db_connection_pool_exhaustion | 0.55 | 68s | 4 | escalate (0.00) | never | correct action (revised later) |
| 4 | db_disk_full | APILatencyHigh, DBDiskNearFull, ErrorRateHigh | db_disk_full | 1.00 | 124s | 4 | escalate (0.00) | 124s | correct action (revised later) |
| 5 | db_connection_pool_exhaustion | DBPoolNearExhaustion | db_connection_pool_exhaustion | 0.55 | 60s | 2 | escalate (0.00) | never | correct action (revised later) |
| 6 | db_connection_pool_exhaustion | DBPoolNearExhaustion | db_connection_pool_exhaustion | 0.55 | 66s | 2 | escalate (0.00) | never | correct action (revised later) |
| 7 | high_memory_usage | ContainerMemoryHigh | high_memory_usage | 0.60 | 82s | 5 | escalate (0.00) | 142s | correct action (revised later) |
| 8 | db_connection_pool_exhaustion | DBPoolNearExhaustion | db_connection_pool_exhaustion | 0.55 | 76s | 4 | escalate (0.00) | 136s | correct action (revised later) |
| 9 | high_memory_usage | ContainerMemoryHigh | high_memory_usage | 0.60 | 81s | 5 | escalate (0.00) | 141s | correct action (revised later) |
| 10 | high_memory_usage | ContainerMemoryHigh | high_memory_usage | 0.60 | 84s | 3 | escalate (0.00) | 144s | correct action (revised later) |
| 11 | db_disk_full | DBDiskNearFull | db_disk_full | 0.60 | 140s | 2 | db_disk_full (0.60) | never | correct action |
| 12 | unknown | APILatencyHigh, CPUThrottlingHigh | escalate | 0.00 | 144s | 2 | escalate (0.00) | never | correct escalation |
| 13 | db_disk_full | APILatencyHigh, DBDiskNearFull, ErrorRateHigh | db_disk_full | 1.00 | 139s | 3 | escalate (0.00) | 139s | correct action (revised later) |
| 14 | db_disk_full | APILatencyHigh, DBDiskNearFull, ErrorRateHigh | db_disk_full | 1.00 | 122s | 4 | escalate (0.00) | 122s | correct action (revised later) |
| 15 | db_disk_full | DBDiskNearFull | db_disk_full | 0.60 | 77s | 3 | escalate (0.00) | 137s | correct action (revised later) |
| 16 | high_memory_usage | APILatencyHigh, ContainerMemoryHigh | high_memory_usage | 0.75 | 120s | 2 | escalate (0.00) | 120s | correct action (revised later) |
| 17 | unknown | ErrorRateHigh | escalate | 0.00 | 101s | 2 | escalate (0.00) | never | correct escalation |
| 18 | high_memory_usage | ContainerMemoryHigh | high_memory_usage | 0.60 | 85s | 2 | high_memory_usage (0.75) | 145s | correct action |

## Timing, known-cause trials

- first incident reaches the engine: median **83s** (range 60-140s) after injection
- first decision a dispatcher gated at 0.75 would act on: median **138s** (range 120-145s), in 10 of 14 trials; **never reached the gate** in trials #2, #5, #6, #11
- the gap between the two is the cost of waiting for the consequences to fire: the deciding symptom alerts first and alone, so the first webhook usually scores about 0.55-0.60.

### First decision per trial

What the dispatcher would act on: the first webhook of each trial. REAL pipeline, INJECTED faults.

**17 incidents.** Accuracy: **100.0%**

#### Outcomes

| outcome | count | share |
|---|---:|---:|
| correct action | 14 | 82.4% |
| correct escalation | 3 | 17.6% |
| missed (escalated a known cause) | 0 | 0.0% |
| WRONG ACTION | 0 | 0.0% |

#### Confusion matrix

| true \ predicted | db connection pool exhaustion | db disk full | high memory usage | escalate |
|---|---:|---:|---:|---:|
| **db connection pool exhaustion** | 4 | 0 | 0 | 0 |
| **db disk full** | 0 | 5 | 0 | 0 |
| **high memory usage** | 0 | 0 | 5 | 0 |
| **escalate** | 0 | 0 | 0 | 3 |

#### Per class

| class | precision | recall | F1 | support |
|---|---:|---:|---:|---:|
| db_connection_pool_exhaustion | 100.0% | 100.0% | 100.0% | 4 |
| db_disk_full | 100.0% | 100.0% | 100.0% | 5 |
| high_memory_usage | 100.0% | 100.0% | 100.0% | 5 |
| escalate | 100.0% | 100.0% | 100.0% | 3 |

#### What a confidence gate buys

An action is taken only if a cause was named *and* confidence >= the gate; everything else escalates to a human.

| min confidence | automated actions | wrong actions | precision of actions | known incidents auto-remediated |
|---:|---:|---:|---:|---:|
| 0.00 | 14 (82.4%) | 0 (0.0%) | 100.0% | 100.0% |
| 0.50 | 14 (82.4%) | 0 (0.0%) | 100.0% | 100.0% |
| 0.75 | 4 (23.5%) | 0 (0.0%) | 100.0% | 28.6% |
| 0.90 | 3 (17.6%) | 0 (0.0%) | 100.0% | 21.4% |

#### Diagnostics

- mean confidence when correct: 0.68
- mean confidence when wrong: n/a (no wrong actions)
- engine time per incident: 11 microseconds

### Every record

All webhooks received, including later updates that carry more symptoms.

**52 incidents.** Accuracy: **61.5%**

#### Outcomes

| outcome | count | share |
|---|---:|---:|
| correct action | 25 | 48.1% |
| correct escalation | 7 | 13.5% |
| missed (escalated a known cause) | 20 | 38.5% |
| WRONG ACTION | 0 | 0.0% |

#### Confusion matrix

| true \ predicted | db connection pool exhaustion | db disk full | high memory usage | escalate |
|---|---:|---:|---:|---:|
| **db connection pool exhaustion** | 7 | 0 | 0 | 5 |
| **db disk full** | 0 | 8 | 0 | 8 |
| **high memory usage** | 0 | 0 | 10 | 7 |
| **escalate** | 0 | 0 | 0 | 7 |

#### Per class

| class | precision | recall | F1 | support |
|---|---:|---:|---:|---:|
| db_connection_pool_exhaustion | 100.0% | 58.3% | 73.7% | 12 |
| db_disk_full | 100.0% | 50.0% | 66.7% | 16 |
| high_memory_usage | 100.0% | 58.8% | 74.1% | 17 |
| escalate | 25.9% | 100.0% | 41.2% | 7 |

#### What a confidence gate buys

An action is taken only if a cause was named *and* confidence >= the gate; everything else escalates to a human.

| min confidence | automated actions | wrong actions | precision of actions | known incidents auto-remediated |
|---:|---:|---:|---:|---:|
| 0.00 | 25 (48.1%) | 0 (0.0%) | 100.0% | 55.6% |
| 0.50 | 25 (48.1%) | 0 (0.0%) | 100.0% | 55.6% |
| 0.75 | 13 (25.0%) | 0 (0.0%) | 100.0% | 28.9% |
| 0.90 | 5 (9.6%) | 0 (0.0%) | 100.0% | 11.1% |

#### Diagnostics

- mean confidence when correct: 0.73
- mean confidence when wrong: n/a (no wrong actions)
- engine time per incident: 9 microseconds
