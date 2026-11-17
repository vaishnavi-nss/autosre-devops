# Test report: what was run on the real cluster, what it showed, what is still open

Branch `devops-completion`. Cluster: Minikube (containerd) on the AWS Academy lab EC2 instance (`i-04f8f4d75378a1aba`), isolated `autosre` namespace,
2026-11-16 and 2026-11-17 (comp11 to comp14 ran on 2026-11-17, 09:02 to 12:09 UTC). Raw results: `s3://autosre-validation-841736140404/results-comp3/` to `results-comp14/` (`<run>.log`, `report.md`, `scenarios.json`).
Unit tests: **275 passed** (`pytest tests correlation-engine/tests second-opinion/tests`); the baseline on `devops-integration` was 150. The unit tests use a fake
cluster and a stubbed `ansible-playbook`; they do not show that a playbook works. Only the runs below do.

**Nothing here is simulated recovery, and since comp13 no fault injects a consequence.** Pool exhaustion leaks every slot of the transaction service's bounded
connection pool (requests really time out with HTTP 503). A full disk really fills the database's bounded log directory until writes fail with HTTP 507. Memory
holds real memory in the container, and **every request then does real work on that heap** (a CRC over every leaked byte, in a worker thread; no sleep), so it is
really slower. How slow depends on the CPU the pod gets, so the runner measures a transaction and only raises the intensity if the median is under 0.7 s (it was
1.3 s with the default, so it never needed to). The one thing the runner still sets is the **intensity** of each fault (how much memory, how many slots, how full the
disk), the way a load test sets a request rate. The comp6 to comp10 rows used older fault models (pool gauge pinned plus injected errors, memory slowness as an
injected sleep) and are kept for history.

## Live results (AUTO_REMEDIATE on for the run only; restored and confirmed off afterwards)

| Scenario | Run | Outcome | Alert after | Incident after | Cause | Confidence | ML | Playbook | Recovered (independent check) |
|---|---|---|---:|---:|---|---:|---|---|---|
| **memory (real slowness)** | comp12, trial 1 | **passed** (the run was then stopped, see below) | | | high_memory_usage | 0.75 | agree | `scale_replicas.yml` | yes |
| **memory (real slowness)** | comp13, trial 1 | **escalated by policy**, nothing executed | 56 s | | high_memory_usage | 0.75 | **disagree** (scorer: api-gateway, gateway_fault, 0.81) | none | n/a |
| **memory (real slowness)** | comp13, trial 2 | **passed** | 51 s | 143 s | high_memory_usage | 0.75 | agree | `scale_replicas.yml` | yes |
| **memory (real slowness)** | comp13, trial 3 | **passed** | 51 s | 51 s | high_memory_usage | 0.75 | agree | `scale_replicas.yml` | yes |
| **pool (real)** | comp13, trial 1 | **passed** | 36 s | 128 s | db_connection_pool_exhaustion | 0.85 | agree | `restart_service.yml` | yes |
| **pool (real)** | comp13, trial 2 | **passed** | 36 s | 158 s | db_connection_pool_exhaustion | 0.85 | agree | `restart_service.yml` | yes |
| **pool (real)** | comp14 | **passed** | 36 s | 173 s | db_connection_pool_exhaustion | 0.85 | agree | `restart_service.yml` | yes |
| **disk (real)** | comp13, trial 1 | **passed** | 72 s | 102 s | db_disk_full | 0.85 | agree | `clear_logs.yml` | yes |
| **disk (real)** | comp13, trial 2 | **passed** | 87 s | 118 s | db_disk_full | 0.85 | agree | `clear_logs.yml` | yes |
| **cooldown (real pool fault)** | comp14 | **passed**: the same fault again was `skipped/cooldown`; executions 1 to 1 | | | db_connection_pool_exhaustion | 0.85 | agree | none started | n/a |
| **unknown cause** | comp14 | **passed**: escalated to a human, nothing executed | 101 s | n/a | none | n/a | n/a | none | n/a |
| **guardrails, live** | comp14 | **passed**: 6 of 6 held, no execution started | | | | | | | |
| **pool (real)** | comp11, trial 1 | **passed** | 41 s | 148 s | db_connection_pool_exhaustion | **0.85** | agree | `restart_service.yml` | yes |
| **pool (real)** | comp11, trial 2 | **passed** (repeat) | 46 s | 133 s | db_connection_pool_exhaustion | **0.85** | agree | `restart_service.yml` | yes |
| **disk (real, writes fail with 507)** | comp11, trial 1 | **passed** | 72 s | 103 s | db_disk_full | **0.85** | agree | `clear_logs.yml` | yes |
| **disk (real, writes fail with 507)** | comp11, trial 2 | **passed** (repeat) | 82 s | 112 s | db_disk_full | **0.85** | agree | `clear_logs.yml` | yes |
| pool (older model) | comp6 | **passed** | 56 s | 118 s | db_connection_pool_exhaustion | 0.75 | agree | `restart_service.yml` | yes |
| pool (older model) | comp9 | **passed** (reproduced) | 51 s | 128 s | db_connection_pool_exhaustion | 0.75 | agree | `restart_service.yml` | yes |
| cooldown | comp6, comp9 | **passed**: the same fault again was `skipped/cooldown`; executions 1 to 1 | | | db_connection_pool_exhaustion | 0.75 | agree | none started | n/a |
| **memory** | comp10 | **passed** | 56 s | 128 s | high_memory_usage | 0.75 | agree | `scale_replicas.yml` | yes |
| **disk** (older model) | comp10 | **passed** | 66 s | 82 s | db_disk_full | 0.75 | agree | `clear_logs.yml` | yes (disk ratio back to 0.30) |
| unknown cause | comp4, comp10 | **passed**: escalated to a human, nothing executed | 91 s | n/a | none | n/a | n/a | none | n/a |
| guardrails (7 checks dry, 6 live) | comp4, comp6, comp8 to comp10 | **passed** every time, no execution started | | | | | | | |

"Independent check" means the runner, not the dispatcher, confirmed recovery: the deployment is fully ready, `/health` is ok, and the alert resolved (pool, disk).
For memory the runner requires **all** of: three replicas ready, `ContainerMemoryHigh` resolved (it sums memory across pods, so three pods with one leaking one drop it
back under 0.9; my earlier note that it would persist was an assumption that these runs disproved), and 24 live transactions through the service with **no errors and a
median under 0.3 s**. The leaking pod still holds its memory and stays slow, so this is recovery of the service, not a cure of the leak: a human still has to replace
that pod (the runner does it itself after each trial). The dispatcher also verifies each run itself (rollout, `/health`, disk metric) before it reports `succeeded`.

### Reliability, counted (live, real faults, safe defaults restored and confirmed after every run)

| Scenario | Trials | Acted and recovered | Escalated by policy (safe) | Failed |
|---|---|---:|---:|---:|
| pool (real) | 5 (comp11 x2, comp13 x2, comp14) | **5** | 0 | 0 |
| disk (real) | 4 (comp11 x2, comp13 x2) | **4** | 0 | 0 |
| memory (real slowness) | 4 (comp12 x1, comp13 x3) | **3** | **1** | 0 |
| cooldown | 1 on the real pool fault (comp14; 2 on the older model) | 1 | n/a | 0 |
| unknown cause | 1 (comp14; 2 earlier) | 1 escalated | n/a | 0 |
| guardrails, live | 6 checks (comp14; earlier runs 6 of 6 as well) | 6 held | n/a | 0 |

These are small counts, not a reliability rate: 12 faults acted on correctly out of 13, and the one that did not act was the second opinion saying no. Run-to-run spread:
pool alert after 36 to 46 s and incident after 128 to 173 s; disk 72 to 87 s and 102 to 118 s; memory 51 to 56 s.

**Correction (made after I was asked to deploy the cluster-trained model): the scorer in these runs was already that model.** An earlier version of this
report said the scorer was the RCAEval benchmark model. It was not. `second-opinion/Dockerfile` sets `MODEL_PATH=models/own.joblib`, and the verdicts name this cluster's own
causes (`db_connection_pool_exhaustion`, `high_memory_usage`), which the benchmark model cannot output (its classes are `cpu, delay, disk, loss, mem`; checked by loading both).
So every second opinion above came from the model trained on this cluster's incidents (53 labelled trials from the earlier live runs 3, 4, 5-early and 8; its own
leave-one-trial-out check gave 95 % service accuracy). Nothing needed deploying.

**What the runs add, and the one disagreement.** That model was trained on the *older* fault shapes (injected latency for memory, a pinned gauge for the pool). The real faults
here are new to it, so these runs are a genuine held-out test: it agreed on 12 of 13 live faults (pool 5/5, disk 4/4, memory 3/4). In the memory miss (comp13 trial 1) the rules
named `transaction-service` (0.75; `ContainerMemoryHigh` plus `APILatencyHigh`) and the scorer put the fault on `api-gateway` (`gateway_fault`, confidence 0.81). A really slow
transaction service makes the gateway's own latency breach too, and a model that learned memory faults from a milder injected slowness sometimes reads that as a gateway fault.
The gate did what it is for (`Second opinion is disagree, policy requires agree`: escalated, nothing executed). I did not change or retrain the model.

**The obvious next step, not done here (the lab session had ended):** retrain it on windows from these real faults (`second-opinion/train_own.py` needs per-trial score tables;
the scenario runner does not record them yet, so that has to be added), re-run its leave-one-trial-out check, redeploy, and repeat the memory trials. Until then, expect
real memory faults to be acted on about three times in four.

### Dry-run and Phase 3 (ML) results, decisions only

| Scenario | Run | Result |
|---|---|---|
| pool / disk | comp4 | `dry_run`: every dispatcher check passed (confidence 0.75 and 0.85, ML agree); nothing executed |
| memory (0.15 s, 0.3 s latency) | comp4, comp8 | escalated by policy: confidence stayed at 0.60. The second opinion **disagreed** in comp4 and agreed in comp8 |
| **two faults, ambiguity rule on** | comp8 | **passed**: memory then pool escalated with "deciding symptoms of 2 causes fired together"; nothing executed |
| **two faults, gate mode on** | comp8 | **passed (recorded)**: the incident was **vetoed**, the second opinion disagreed with the dominant fault; nothing executed |

Together with the earlier evidence (`docs/evaluation.md`, `docs/real-aws-validation/`): the 0.75 threshold holds (a lone deciding symptom never acts), a disagreeing or
missing second opinion blocks execution, two simultaneous causes escalate when the ambiguity rule is on, and the gate vetoes when the scorer disagrees.

### Front end and Grafana, rendered and looked at

Both were opened in a real browser, not only checked through their data. Headless Chromium on the lab instance screenshotted the **deployed** front end
(`kubectl port-forward svc/frontend`) and the **deployed** Grafana dashboard (`svc/monitoring-grafana`); I also served the real page code locally with the exact
`/api/state` JSON captured from the cluster right after a live execution and looked at it in the built-in browser. All 18 chart and stat panels of the dashboard render
with live data. The rendered pages showed four defects that no data check could, all now fixed and covered by tests:

1. The active-alerts panel listed `InfoInhibitor` (a meta alert) as a firing incident with a red badge: the query now excludes `InfoInhibitor` and `Watchdog`.
2. The ledger card stretched into a tall empty block: grid cards no longer stretch.
3. After a successful fix the "Latest incident" panel said **Escalated to a human** (the engine raises an `unmatched` follow-up while alerts are still clearing) above a
   "Fix verified" panel. The page now shows the incident that was acted on when such a follow-up arrives within 5 minutes of it; a newer, unrelated escalation is still shown.
4. In Grafana the three count panels (root causes, executions by result, not run and why) were bar gauges that hid the series name and said "No data" before the first
   execution. They are now stat panels that always show name and value, and read `none yet 0` instead of "No data".

What the rendered pages showed after the fixes: the front end (live page, after a real `restart_service.yml`) showed root cause db_connection_pool_exhaustion, 85 %,
ML agree, playbook accepted, execution succeeded in 33.1 s, the 900 s cooldown counting down, every deployment 1/1, and "Fix verified; alerts still clearing". Grafana showed
"Root causes identified: db_connection_pool_exhaustion 4", p95 latency and 5xx charts with every fault of the day as a spike, replicas going to 3 and back for the memory
scale-outs, and dispatch decisions split into accepted, cooldown and escalated.

Two honest limits: the dispatcher keeps its history and counters **in memory**, so restoring the safe settings (which restarts it) empties the execution list and zeroes the
cumulative count panels (the trend panels keep their history in Prometheus); and the `pickIncident` fix (3) was pushed after comp14 was deployed, so the deployed pod does not have
it yet. It was verified in the browser through the locally served real page.

## Runs, in order

| Run | Mode | What it showed |
|---|---|---|
| comp1, comp2 | dry | Stopped at preflight, by design: second-opinion manifest pinned an unbuildable image tag; then the runner's JSON POST was rejected (HTTP 422) |
| comp3 | dry | Every dispatcher answer was "Confidence below threshold"; the unknown-cause scenario saw no alert because the load generator produced no backend traffic |
| comp4 | dry | First full dry run with those fixes |
| comp5 | live | Dispatcher preflight stopped the run safely: its Kubernetes client was `system:anonymous` (403); nothing was changed |
| comp6 | live | pool and cooldown passed; disk failed on RBAC (`pods/exec` needs `get` as well as `create`) |
| comp7 | live (cancelled) | Cancelled before any fault when the lab had about 11 minutes left; safe defaults forced and confirmed |
| comp8 | dry | memory, two-fault ambiguity and gate scenarios (above) |
| comp9 | live | pool and cooldown passed again; stopped at memory on an HTTP 503 right after a restart (the runner now waits for a healthy pod and retries) and restored |
| comp10 | live | **memory, disk and unknown cause passed** (above) |
| **comp11** | live | **real pool and real disk faults, each twice: 4 of 4 passed** (above). Restore confirmed: `auto_remediate: false`, no errors, all 8 deployments 1/1 |
| comp12 | live (stopped) | First run with the real memory slowness: trial 1 passed. Then it showed a runner bug (below): after the memory trial the scale-down kept the pod that still held the leak, the alert fired again and the dispatcher, correctly, scaled out a second time, which contaminated the next trial. Relaunched as comp13; the launcher's restore ran and safe defaults were confirmed |
| **comp13** | live | memory x3, pool x2, disk x2: **6 passed, 1 escalated by policy** (ML disagree). Restore confirmed |
| **comp14** | live | pool, cooldown, unknown cause, live guardrails: **4 of 4 passed**. Restore confirmed: `auto_remediate: false`, no errors, 8 of 8 deployments 1/1, no runner |

## Bugs that only the real cluster showed (all fixed and covered by a test)

1. `k8s/second-opinion.yaml` pinned a tag no script builds, with `imagePullPolicy: Never`: a clean deploy could never start the scorer.
2. The dispatcher image left `/tmp/.ansible` root-owned, so `ansible-playbook` failed as the non-root user.
3. The load generator fetched the frontend's static page, so no traffic reached the gateway, transaction service or database; `ErrorRateHigh` and `APILatencyHigh` could never fire.
4. `kubectl create --raw` sends no JSON content type, so the chaos endpoints answered 422 to the runner.
5. A lone deciding symptom scores 0.55 to 0.60, below the 0.75 gate, so the first webhook of every incident is rightly escalated; the scenarios inject the consequence a real fault causes and the runner judges the incident the dispatcher acts on.
6. The `kubernetes` Python package 36.0.0 sent requests as `system:anonymous`; the dispatcher now uses REST with its service-account token, and the version Ansible uses is pinned to 31.0.0.
7. `pods/exec` needs `get` as well as `create` (the disk playbook's WebSocket exec).
8. A relaunch killed the previous runner without running its restore, **leaving `AUTO_REMEDIATE=true`**. It was found, switched off and verified. The runner now restores on SIGTERM; the launcher waits for the old runner and forces the safe defaults before and after `kubectl apply`.
9. The transaction service hard-coded a pool maximum of 10 while its manifest said 20.
10. Image checks must run inside the pod (this Minikube uses containerd): `ansible-core 2.21.6`, `kubernetes.core 6.6.0` and all three playbooks pass `--syntax-check` there.
11. A chaos call right after a restart got HTTP 503 and stopped the whole run; the runner now waits for a healthy target and retries transient 5xx.

12. (comp12) After a memory trial the runner scaled `transaction-service` back to one replica, which keeps an arbitrary pod, possibly the one holding the leak. The memory alert fired again and the dispatcher scaled out a second time. The runner now scales down **and replaces the pods** (`cleanup_memory_fault`).
13. The rendered front end and Grafana showed the four defects listed above (meta alerts, stretched card, follow-up noise over the acted-on incident, bar gauges without names or zeros).
14. A headless-browser install overlapped trial 1 of comp13, so that trial's timings are slightly noisier; its outcome (ML disagree) was a scorer decision, not a timeout.

## Acceptance criteria, honestly

| # | Criterion | Status |
|---|---|---|
| 1 | All three faults can be injected | Yes: pool (real: leaked slots), memory (real memory, injected slowness), disk (real bytes, real write failures) |
| 2 | Prometheus detects the correct symptoms | Yes: 51 to 91 s |
| 3 | The engine identifies the correct root cause | Yes, all three, at confidence 0.75 to 0.85 |
| 4 | The ML second opinion produces a recorded verdict | Yes, on every incident (agree on all passing live runs) |
| 5 | The dispatcher selects the correct approved playbook | Yes: restart, scale, clear logs |
| 6 | Ansible executes the intended operation | **Yes, all three, live** |
| 7 | Kubernetes and application health confirm recovery | **Yes, all three**, by an independent check (memory = capacity ready and healthy) |
| 8 | Grafana displays the incident and recovery evidence | **Yes**: the deployed dashboard and front end were rendered and looked at; defects found there were fixed (one fix, the front end's incident choice, is pushed but not yet deployed) |
| 9 | Unknown and unsafe incidents escalate to a human | Yes: unknown-cause scenario, guardrails, cooldown, two-fault ambiguity, gate veto |
| 10 | Automated tests pass; deployment documented | Yes (265 tests; `01-audit.md`, `02-deploy-demo-cleanup-rollback.md`, this report) |

## Limits, and what was done about each

| Limit (from the previous version of this report) | What was done | Status |
|---|---|---|
| Memory's slowness was injected; a scale-out leaves the leak | The slowness is now real work on the leaked heap (no sleep), calibrated by measuring. The "alert persists" assumption was wrong: the alert sums across pods and resolves, and recovery now requires that plus 24 live transactions with no errors and a median under 0.3 s. The leak itself stays in the original pod by design (the approved playbook is a scale-out); the runner replaces that pod after each trial | **Verified live** (comp12, comp13): 3 of 4 acted and recovered. **Still true:** a scale-out does not cure a leak, a human must replace the pod |
| One passing run per scenario, not a rate | pool 5/5, disk 4/4, memory 3 acted of 4 (1 escalated by the ML gate), cooldown, unknown cause and live guardrails each repeated against the real fault models | **Counted, still small.** These are not confidence intervals. Memory acts on only about three in four real faults while the cluster-trained scorer (trained on the older injected shapes) is the gate |
| Front end and Grafana pages not viewed | Both rendered in a real browser from the deployed cluster; four defects found and fixed | **Done**, except that the last front-end fix is not deployed yet |
| memory, cooldown, guardrails, unknown cause not repeated against the new fault models | All four were run against them (comp13, comp14) | **Done** |

New things this work found and did **not** fix:
- The scorer's disagreement on real memory slowness (above). The model in use is already the cluster-trained one; retraining it on the new real-fault data is the next step and needs per-trial score tables the runner does not record yet.
- The dispatcher's execution history and counters are in memory, so any dispatcher restart empties the dashboard's execution panels.
- Lab caveats: the instance runs a single-node Minikube on 2 vCPUs, so timings are not production numbers, and every fault is started by the runner.

Cluster state at the end: `AUTO_REMEDIATE=false`, `MIN_CONFIDENCE=0.75`, 8 of 8 deployments 1/1, no runner process. The lab instance and the S3 bucket were left in place, as agreed.
