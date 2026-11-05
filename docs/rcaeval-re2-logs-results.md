# RCAEval RE2 evaluation (first 300s after injection, with logs)

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
| Online Boutique | 90 | 91.1% | 82.2% | 73.3% | 26.7% |
| Sock Shop | 90 | 95.6% | 68.9% | 65.6% | 34.4% |
| Train Ticket | 90 | 92.2% | 52.2% | 50.0% | 50.0% |

Pooled over all held-out folds: 270 cases, both right **63.0%**.

By fault, both right:

| fault | cases | both right |
|---|---:|---:|
| cpu | 45 | 66.7% |
| mem | 45 | 62.2% |
| disk | 45 | 80.0% |
| delay | 45 | 66.7% |
| loss | 45 | 40.0% |
| socket | 45 | 62.2% |

Fault confusion (rows = true, columns = predicted; service ignored):

| true / predicted | cpu | mem | disk | delay | loss | socket |
|---|---:|---:|---:|---:|---:|---:|
| **cpu** | 30 | 6 | 1 | 0 | 0 | 8 |
| **mem** | 1 | 28 | 15 | 0 | 0 | 1 |
| **disk** | 0 | 6 | 36 | 0 | 1 | 2 |
| **delay** | 0 | 0 | 0 | 37 | 8 | 0 |
| **loss** | 1 | 0 | 1 | 12 | 24 | 7 |
| **socket** | 11 | 4 | 1 | 0 | 1 | 28 |

### Does confidence mean anything?

Expected calibration error: **0.129** (0 = confidence equals accuracy). Mean confidence 0.58, observed accuracy 0.63.

Acting only above a confidence gate (pooled held-out cases):

| gate | acts on | wrong actions | of all cases wrong | precision of actions |
|---:|---:|---:|---:|---:|
| 0.0 | 270 (100%) | 100 | 37.0% | 63.0% |
| 0.5 | 184 (68%) | 58 | 21.5% | 68.5% |
| 0.6 | 123 (46%) | 38 | 14.1% | 69.1% |
| 0.7 | 61 (23%) | 23 | 8.5% | 62.3% |
| 0.8 | 33 (12%) | 14 | 5.2% | 57.6% |
| 0.9 | 16 (6%) | 3 | 1.1% | 81.2% |
