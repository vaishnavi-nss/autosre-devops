# Runs 16 and 17: the ambiguity rule and the gate, measured on and off

Two open decisions, `ESCALATE_ON_AMBIGUITY` and `SECOND_OPINION_MODE=gate`, had only been judged from the simulator and
from shadow-mode logs. These sessions ran them live, with two real faults injected together. Session 2026-11-12/13,
fresh lab, scorer `b565d77f`, service CPU limit 1000m (see `docs/live-validation-run15/summary.md`), commit `1fd4eea`.
Raw results: `s3://autosre-validation-841736140404/results-run16/` and `results-run17/`. Each run's `conditions.json`
records the settings; the harness put the engine back to **shadow with the rule off** when each run ended (both logs
say so), and `k8s/` was not changed. After run 17 the four services' CPU limits were set back to 200m by hand (the harness's
`--cpu-limit` does not undo itself) and the engine's environment read back as `SECOND_OPINION_MODE=shadow` with no
ambiguity setting.

**Not gate evidence.** Pair trials have two real faults, so `gate_check.py` skips them. The 3 single-fault controls
in run 16 are the only part that counts toward the gate, and they are too few to change its verdict.

## What was run

| run | engine settings | trials | length |
|---|---|---|---|
| 16 | gate on, rule **off** | 9 pair trials (3 each of disk+memory, disk+pool, memory+pool) and 3 single-fault controls | 113 min |
| 17 | gate on, rule **on** | 6 pair trials (2 each) | 51 min |

No trial was contaminated. "Acting on one of the two" is partial, not wrong: both faults were real. A wrong action
would name a cause that is not in the pair; **there were none in either run.**

## 1. The ambiguity rule

**Cost, on real single-fault incidents (no lab time).** The engine's `correlate()` was replayed over every recorded
webhook of runs 11-15 (130 webhooks from the counted trials) with the rule off and on
(`evaluation/ambiguity_report.py cost`). **The rule changed nothing**: no correct action became an escalation and no
wrong action was removed (51 correct, 0 wrong, 73 missed, 6 escalated either way); the same on the 34 first webhooks
of each trial (28 correct, 1 missed, 5 escalated either way). On single faults the rule is free. (An earlier version of
this report said 195 webhooks and 21 wrong ones; that was a bug in the report, corrected below.)

**Benefit, on two real faults.**

| | rule off (run 16) | rule on (run 17) |
|---|---|---|
| pair trials | 9 | 6 |
| trials where the engine acted on one fault at its first webhook | **9 of 9** | **0 of 6** |
| webhooks | 18 | 14 |
| acted on a cause in the pair | 15 | 0 |
| escalated by the rules | 0 | 12 |
| withheld by the gate | 3 | 2 |
| acted on a cause not in the pair | 0 | 0 |

With the rule off the engine named the deepest fault (disk in every disk pair; memory first in memory+pool) and the other
fault went unmentioned. With it on, 12 of 14 webhooks escalated at once. The other 2 were first webhooks of disk+pool
trials in which only the pool's deciding symptom had fired yet, so the rule could not see two causes; those were
withheld by the gate (below). Replaying the rule over run 16's recorded alerts would have escalated all 18 of its
webhooks; run 17 let 2 through only because of that timing.

**Reading.** The rule is free on single faults and, on two faults, turns every "act on one" into an escalation. That
is a safety trade, not an accuracy gain: whether acting on one of two real faults is better than paging a person is a
risk judgement. What was **not** measured: what the remediation does to the second fault, and how often two real
faults occur in practice.

## 2. The gate

- **Single faults (3 controls, run 16):** 0 vetoes of 7 correct decisions, 6 of 6 true claims backed, 6 of 6 wrong
  claims caught. With the earlier sessions this is 0 vetoes in every post-fix decision.
- **Two faults:** the scorer disagreed with the absent third cause in **30 of 30** probes, agreed with the disk fault in
  20 of 20, but disagreed with the **pool** fault in **20 of 20** and with the **memory** fault in 10 of 20, though both
  were real. It reads the dominant fault and not the second.
- **Effect:** with the rule off the gate withheld 3 of 18 webhooks, all of them the engine naming the pool in a
  memory+pool trial (the scorer disagreeing with a real fault, so those are false vetoes in the narrow sense, though
  escalating a two-fault incident is the safe outcome). It did **not** stop the 15 actions on the disk or memory
  fault. With the rule on it withheld the 2 pool-only first webhooks the rule could not see.

**Reading.** The gate on its own does not protect against two simultaneous faults, because the scorer agrees with
whichever fault is dominant. It is a backstop beside the rule, not a substitute.

## What it suggests, and what it does not

- Turning the ambiguity rule on costs nothing measured on single faults and escalates exactly the two-fault cases.
  The data favours switching it on.
- The gate stayed harmless on single faults and caught the cases the rule missed, but its own protection against two
  faults is weak. It remains the team's decision, still one cause at a time, `no_opinion` as pass-through.
- Small samples: 15 pair trials, 3 pairs, synthetic faults on a small cluster, 1000m limits (not the 200m behind the
  gate evidence). `SECOND_OPINION_MODE` is `shadow` and the rule is off in the cluster, as found.

## A correction to the analysis, not a finding about the harness

The first version of `ambiguity_report.py` dropped contaminated trials *before* assigning recorded incidents to trials.
Each contaminated trial's incidents then went to the counted trial in front of it, which showed up as 65 extra
webhooks and **21 "wrong" ones** (for example a contaminated memory trial's `ContainerMemoryHigh` credited to the disk
trial before it). I read those as alerts leaking in from a neighbouring trial; that was wrong. The leak view showed
every one began 13-40 minutes *after* the trial's injection, which is when the neighbouring trial ran.
With the assignment fixed (all trials first, then filter; `_session()` in the script, with a test), **no webhook in
runs 11-15 names a cause other than its trial's label** (`ambiguity_report.py wrong` and `leaks` are empty). There is
no harness-isolation problem in this data and nothing to fix there. `gate_check.py` was never affected: it assigns
over every trial and filters afterwards.
