# RCAEval RE2 evaluation (first 300s after injection)

270 cases, 3 systems. The faults are real injections; the diagnosers see only metric scores, never the labels.

## Replay baseline (threshold 8, chosen on Online Boutique only)

| system | cases | correct action | correct escalation | missed | wrong action |
|---|---:|---:|---:|---:|---:|
| Online Boutique (tuned here) | 90 | 55 | 0 | 0 | 35 |
| Sock Shop | 90 | 39 | 0 | 1 | 50 |
| Train Ticket | 90 | 42 | 0 | 4 | 44 |

By fault (all systems):

| fault | cases | correct action | correct escalation | missed | wrong action |
|---|---:|---:|---:|---:|---:|
| cpu | 45 | 40 | 0 | 1 | 4 |
| mem | 45 | 25 | 0 | 0 | 20 |
| disk | 45 | 31 | 0 | 0 | 14 |
| delay | 45 | 25 | 0 | 0 | 20 |
| loss | 45 | 0 | 0 | 3 | 42 |
| socket | 45 | 15 | 0 | 1 | 29 |

## Learned model (leave one system out)

Trained on the other two systems, tested on the held-out one.

| held-out system | cases | service top-1 | fault accuracy | both right | wrong-action rate at gate 0.0 |
|---|---:|---:|---:|---:|---:|
| Online Boutique | 90 | 92.2% | 77.8% | 72.2% | 27.8% |
| Sock Shop | 90 | 95.6% | 68.9% | 65.6% | 34.4% |
| Train Ticket | 90 | 91.1% | 54.4% | 53.3% | 46.7% |

Pooled over all held-out folds: 270 cases, both right **63.7%**.

By fault, both right:

| fault | cases | both right |
|---|---:|---:|
| cpu | 45 | 71.1% |
| mem | 45 | 62.2% |
| disk | 45 | 77.8% |
| delay | 45 | 64.4% |
| loss | 45 | 40.0% |
| socket | 45 | 66.7% |

Fault confusion (rows = true, columns = predicted; service ignored):

| true / predicted | cpu | mem | disk | delay | loss | socket |
|---|---:|---:|---:|---:|---:|---:|
| **cpu** | 32 | 6 | 0 | 0 | 0 | 7 |
| **mem** | 1 | 28 | 15 | 0 | 0 | 1 |
| **disk** | 0 | 7 | 35 | 0 | 1 | 2 |
| **delay** | 0 | 0 | 0 | 34 | 11 | 0 |
| **loss** | 0 | 0 | 1 | 15 | 22 | 7 |
| **socket** | 10 | 3 | 1 | 1 | 0 | 30 |

### Does confidence mean anything?

Expected calibration error: **0.103** (0 = confidence equals accuracy). Mean confidence 0.56, observed accuracy 0.64.

Acting only above a confidence gate (pooled held-out cases):

| gate | acts on | wrong actions | of all cases wrong | precision of actions |
|---:|---:|---:|---:|---:|
| 0.0 | 270 (100%) | 98 | 36.3% | 63.7% |
| 0.5 | 180 (67%) | 56 | 20.7% | 68.9% |
| 0.6 | 115 (43%) | 32 | 11.9% | 72.2% |
| 0.7 | 37 (14%) | 11 | 4.1% | 70.3% |
| 0.8 | 20 (7%) | 5 | 1.9% | 75.0% |
| 0.9 | 11 (4%) | 2 | 0.7% | 81.8% |
