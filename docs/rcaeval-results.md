# RCAEval RE1 evaluation (first 300s after injection)

373 cases, 3 systems. The faults are real injections; the diagnosers see only metric scores, never the labels.

## Replay baseline (threshold 100, chosen on Online Boutique only)

| system | cases | correct action | correct escalation | missed | wrong action |
|---|---:|---:|---:|---:|---:|
| Online Boutique (tuned here) | 123 | 63 | 17 | 30 | 13 |
| Sock Shop | 125 | 45 | 5 | 15 | 60 |
| Train Ticket | 125 | 30 | 25 | 37 | 33 |

By fault (all systems):

| fault | cases | correct action | correct escalation | missed | wrong action |
|---|---:|---:|---:|---:|---:|
| cpu | 74 | 40 | 0 | 31 | 3 |
| mem | 75 | 35 | 0 | 7 | 33 |
| disk | 75 | 0 | 47 | 0 | 28 |
| delay | 75 | 63 | 0 | 8 | 4 |
| loss | 74 | 0 | 0 | 36 | 38 |

## Learned model (leave one system out)

Trained on the other two systems, tested on the held-out one.

| held-out system | cases | service top-1 | fault accuracy | both right | wrong-action rate at gate 0.0 |
|---|---:|---:|---:|---:|---:|
| Online Boutique | 123 | 88.6% | 76.4% | 67.5% | 32.5% |
| Sock Shop | 125 | 91.2% | 64.0% | 59.2% | 40.8% |
| Train Ticket | 125 | 92.8% | 60.0% | 56.8% | 43.2% |

Pooled over all held-out folds: 373 cases, both right **61.1%**.

By fault, both right:

| fault | cases | both right |
|---|---:|---:|
| cpu | 74 | 33.8% |
| mem | 75 | 98.7% |
| disk | 75 | 65.3% |
| delay | 75 | 65.3% |
| loss | 74 | 41.9% |

Fault confusion (rows = true, columns = predicted; service ignored):

| true / predicted | cpu | mem | disk | delay | loss |
|---|---:|---:|---:|---:|---:|
| **cpu** | 26 | 0 | 48 | 0 | 0 |
| **mem** | 0 | 74 | 0 | 1 | 0 |
| **disk** | 21 | 0 | 49 | 1 | 4 |
| **delay** | 0 | 0 | 1 | 52 | 22 |
| **loss** | 2 | 0 | 3 | 21 | 48 |

### Does confidence mean anything?

Expected calibration error: **0.115** (0 = confidence equals accuracy). Mean confidence 0.53, observed accuracy 0.61.

Acting only above a confidence gate (pooled held-out cases):

| gate | acts on | wrong actions | of all cases wrong | precision of actions |
|---:|---:|---:|---:|---:|
| 0.0 | 373 (100%) | 145 | 38.9% | 61.1% |
| 0.5 | 193 (52%) | 60 | 16.1% | 68.9% |
| 0.6 | 55 (15%) | 21 | 5.6% | 61.8% |
| 0.7 | 7 (2%) | 6 | 1.6% | 14.3% |
| 0.8 | 0 | 0 | 0.0% | n/a |
| 0.9 | 0 | 0 | 0.0% | n/a |
