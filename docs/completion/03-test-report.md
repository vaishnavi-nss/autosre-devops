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
named `transaction-service` (0.75; `ContainerMemoryHigh` plus `APILatencyHigh`) and the scorer put the fault on `api-gateway` (`gateway_fault`, confidence 0.81). The gate did what it
is for (`Second opinion is disagree, policy requires agree`: escalated, nothing executed).

### Retraining the scorer on real-fault trials: tried, no measurable gain (2026-11-17/18)

I added what retraining needs: `run_scenarios.py --collect` (dry run only, so each fault stays in place) records the scorer's score table at four moments per fault, labelled with the
cause that was really injected, in the format `train_own.py` reads; `/features` now accepts the true `fault_start`; `eval_real.py` compares models; the launcher takes
`COLLECT=1` and `SCORER_MODEL=<file>`. The pipeline was first checked by retraining on the old data with the image's scikit-learn (1.6.1): it reproduced the shipped model's own
numbers exactly (95.3 % service accuracy; 100 true claims agreed, 2 wrong claims wrongly agreed).

| Test | Data | Current model (`own.joblib`) | Retrained (`own_real.joblib`) |
|---|---|---|---|
| Real-fault windows, 10 trials (5 memory, 3 pool, 2 disk), 40 probes | `docs/completion/real-fault-trials` | **40 of 40** right service and fault; every true claim agreed; every wrong claim rejected | **40 of 40** under leave-one-real-trial-out (trained on the old trials plus the other 9) |
| Independent real-fault windows, 8 trials (4 memory, 2 pool, 2 disk), 32 probes, **trained on by neither** | `docs/completion/real-fault-trials-2` | **32 of 32**, same | **32 of 32**, same |
| Live verdicts, dry run with the current model loaded (10 faults, comp "coll1") | the cluster | agreed **10 of 10** (memory 5/5) | n/a |

Counting every live run with the current model, the second opinion agreed on 8 of 9 real memory faults, and on every pool and disk fault. The retrained model also loaded and ran on the cluster (the pod log shows `loaded scorer from models/own_real.joblib`; run "coll2", decisions only, 8 real faults).
**Live verdicts with the retrained model: 8 of 8 agreed** (memory 4/4, pool 2/2, disk 2/2), and the two-fault `pair_gate` check **passed**; `auto_remediate: false` was
confirmed after the run (`results-coll2/report.md`). So the retrained model is also fine live, and also not better.

**What this means.** Retraining did not change anything measurable, because the current model is already right on every real-fault window I could record, including the weak-signal
windows (a fault that follows another within 30 minutes has a polluted baseline, so memory shows as z of about 3 instead of 33) and the clean strong one. So I kept
`own.joblib` as the default and shipped `own_real.joblib` **opt-in only** (`SCORER_MODEL=own_real.joblib`). The earlier "obvious next step, retrain" was a guess that the data does not support.

**Why the one live miss is still unexplained.** My offline windows start at the true fault start; the live engine estimates it from the alert times (`startsAt` minus the rule's `for:`
and a scrape), so its window can begin later and look different. The miss was at the engine's window, not at mine. The way to settle it is to record the scorer's feature table
**with the engine's own estimated `fault_start`**, which needs an engine change or a replay of its estimate in the runner; it is not done. One miss in nine real memory faults is
too few to say whether it is a rare scorer error or a windowing artefact.

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

**Fixed afterwards (run "cloud2", 2026-11-18).** The dispatcher now appends each finished execution to a log on a small persistent volume (`EXEC_LOG=/data/executions.jsonl`, 64 Mi PVC) and reloads it, and any unexpired cooldown, at startup. Evidence from the cloud: pool and disk were run live (**2 passed, none failed**), the restore then restarted the dispatcher, and the new pod came back with `/data/executions.jsonl` holding 2 records and the disk-fix cooldown still counting down (95 s left of 600 s, matching a fix that finished at 00:05:41). `AUTO_REMEDIATE` was confirmed `false` afterwards and 8 of 8 deployments were 1/1. The `pickIncident` fix is also deployed now: the deployed Telemetry page shows the acted-on incident (db_disk_full, ML agree, clear_logs.yml, accepted) and both executions (clear_logs.yml 32 s, restart_service.yml 34.5 s). The cumulative count panels still start from zero after a restart; the trend panels use Prometheus.

### The cloud side, checked from a cold start (2026-11-17, run "cloud1")

The AWS lab had been stopped and restarted, so this checks the whole AWS path from nothing. What it showed:

| Check | Result |
|---|---|
| EC2 instance `i-04f8f4d75378a1aba` | `t3.large`, running, up 4 minutes when checked; 38 GB disk 36 % used; 7.8 GB RAM |
| Systems Manager (how every command reached the instance) | **Online** (agent 3.3.4793.0, Ubuntu 24.04) |
| S3 results bucket | reachable; every run's `report.md`, `scenarios.json`, `trials.jsonl` and log synced (checked for coll1, coll2, cloud1) |
| Minikube after the cold boot | control plane `host: Running`, but **kubelet and apiserver stopped**; the launcher started it unaided and the namespace came back |
| Images rebuilt and redeployed from the branch | all **8** deployments 1/1 |
| Playbooks inside the running dispatcher pod | `kubernetes.core 6.6.0`; **all 3 playbooks pass `--syntax-check`** |
| Second-opinion model | default `models/own.joblib` loaded (pod log), as intended |
| Guardrails (dry) | 7 of 7 held |
| **Pool, live** | passed: alert, diagnosis, second opinion, playbook and independent recovery |
| **Memory, live** | passed, same five confirmations |
| **Disk, live** | passed, same five confirmations |
| Unknown cause | passed: escalated to a human, nothing executed |
| Restore | `AUTO_REMEDIATE` switched off, chaos off, replicas back to 1: **confirmed false**, 8 of 8 deployments 1/1, no runner left |

So the live path (Prometheus, Alertmanager, engine, second opinion, dispatcher, Ansible, Kubernetes recovery) works from a cold start on AWS, 5 of 5 scenarios.

Two things the check turned up, neither a failure of the system:
- The image-check step printed a Python `BrokenPipeError` traceback because it piped `ansible-playbook --version` into `head -1`. It was noise only (the playbook checks after it passed); fixed in the launcher.
- **Three other EC2 instances were running** in the lab account (`t3.medium`, `i-071187d6f97ea8271`, `i-0cf7a8f86041a88c6`, `i-0227adbb1ed3d2519`, not part of AutoSRE), using lab budget. I left them alone as agreed earlier; stopping or terminating them is your call.

### The demo tooling: Grafana, Prometheus, fault console, Jenkins (added 2026-11-18)

To show the live system in a browser, four things were put on the lab instance (all in `scripts/aws/` and `scripts/fault_console/`, pushed to `devops-completion`):

| Piece | What it is | Checked how |
|---|---|---|
| Dashboard, Grafana, Prometheus on :8081, :3000, :9090 | `expose.sh` makes each a systemd service (`kubectl port-forward`, restarts itself) | **Opened in a browser from the user's machine**: the dashboard shows the Telemetry page, Grafana shows the AutoSRE dashboard with the engine's real incidents (6 received, 3 escalated, 2 pool, 1 disk), Prometheus loads with 10 prepared queries |
| Fault console on :8082 | A small web page whose buttons start `run_scenarios.py` for one of pool, memory or disk. **Simulate** injects the real fault with `AUTO_REMEDIATE` left false (decisions only, never reported as recovery). **Live** needs the typed word LIVE and a server switch, turns auto-remediation on for that run only, and the runner restores it. Token needed for every action, one run at a time, Stop button | **17 unit tests** (validation, token, live refusal, one-at-a-time, command built is the runner's own) plus the page loads and the token check was tried on the instance (right token 200, wrong 401). **The buttons have not yet been pressed against the cluster** |
| Jenkins on :8080 | The official `jenkins.war` under systemd, as the user that owns kubectl and docker, job `autosre-cicd` from the repository's `Jenkinsfile`: preflight (refuses during a fault run or if `AUTO_REMEDIATE` is not false), unit tests, build the seven images, deploy, verify (all deployments ready, `AUTO_REMEDIATE` false, smoke test) | Jenkins starts, the sign-in works (HTTP 200 with the admin login). **The pipeline job itself has not been run yet** |

Things this turned up (all fixed): the console's log parser missed the runner's indented outcome lines (a unit test caught it, so the page would have shown no outcome); the Jenkins apt repository's signing key had changed, so the install now runs the official war; current Jenkins needs Java 21, not 17; the install now waits for the apt lock instead of failing; a freshly pushed file is not yet served from GitHub's raw cache, so the instance clones the branch instead.

Access: the user asked for the Jenkins admin password and the fault console token to be the word `password`, and set that on the instance. That is acceptable only because each port is reachable from the user's own IP alone (security-group rules the user added, to be deleted after the demo); both services can change the cluster, so never leave them open or reuse those values.

### Evidence screenshots (2026-11-18)

Saved in `docs/completion/screenshots/`. Taken from the user's own browser against the lab instance, and from GitHub.

| File | What it shows |
|---|---|
| `50-dashboard-executive-idle.jpg` | The deployed dashboard, Executive Overview, healthy and idle |
| `20-grafana-autosre-top.jpg` | Grafana "AutoSRE": confidence 85%, 6 incidents received, 3 escalated, root causes (2 pool, 1 disk), p95 latency and error panels |
| `30-prometheus-up.jpg` | Prometheus: every `autosre` target `up` |
| `10-console-disk-simulate-preparing.jpg`, `11-console-status-unreachable.jpg` | The fault console running a simulated disk fault (phase "preparing", auto-remediation OFF), then the console's status call hanging |
| `40-jenkins-job-before-first-build.jpg` | Jenkins 2.580.2, job `autosre-cicd` present, no builds yet |
| `60` to `69-github-*.jpg` | The repository on GitHub: branch with 61 commits, commit list, docs folder, this report rendered, scripts folder, the Jenkinsfile, the two open pull requests, branches, the ML code folder, the project guide |
| `90-aws-lab-session-expired.jpg` | CloudShell after the lab session ended: the environment no longer exists |
| `91` to `94-aws-*.jpg` | The AWS console after the restart: EC2 instance running (3/3 checks), the S3 results bucket, Systems Manager managed node Online, the security group's inbound rules |
| `100` to `114-sim-*.jpg` | The three simulated runs: console, dashboard Telemetry, Grafana, Prometheus |
| `120` to `128-live-*.jpg` | The three live runs: console passed with auto-remediation ON, playbooks succeeded, Grafana remediation panels |
| `129`, `130` | Final safe state: console idle with auto-remediation OFF, dashboard RECOVERED (VERIFIED), all deployments ready |
| `140-jenkins-signed-out-needs-owner-login.jpg` | Jenkins asking for the owner to sign in |
| `141` to `143-jenkins-*.jpg` | Jenkins build #1: green Stage View, console ending `Finished: SUCCESS`, the console after the deploy |

### The fault console, run end to end (2026-11-18, after the lab was restarted)

The first attempt (01:49 UTC) was cut off when the AWS Academy session expired (no result was claimed from it). The lab was then restarted (new public address 54.198.234.191), the cluster brought back with
`scripts/aws/bring-up.sh` (Minikube started, safe defaults forced, all 8 deployments 1/1, `AUTO_REMEDIATE=false` confirmed before anything ran), and six runs were made **through the fault console's own API**
(`scripts/aws/demo-runs.sh`, token read from the instance, never typed by hand). Times are UTC from the runner's log.

| Run | Mode | Started | Fault injected | Result | What the pipeline did |
|---|---|---|---|---|---|
| pool | simulate | 02:12:36 | 02:14:01 | **dry_run** 02:16:25 | alert fired, cause `db_connection_pool_exhaustion` 85%, ML agree, `restart_service.yml` selected, **nothing executed** |
| memory | simulate | 02:22:33 | 02:23:58 (median transaction 1.226 s) | **dry_run** 02:27:23 | `high_memory_usage` 75%, ML agree, `scale_replicas.yml` selected, nothing executed |
| disk | simulate | 02:33:16 | 02:34:42 | **dry_run** 02:36:09 | `db_disk_full` 85%, ML agree, `clear_logs.yml` selected, nothing executed; restore 02:42:27 |
| pool | **live** | 02:47:50 | 02:49:22 | **passed** 02:52:26 | `restart_service.yml` **succeeded in 29.2 s**; recovery verified independently |
| memory | **live** | 02:57:33 | 02:59:05 (median transaction 1.292 s) | **passed** 03:03:03 | `scale_replicas.yml` **succeeded in 24.4 s**; recovery verified; cooldown for `high_memory_usage` active |
| disk | **live** | 03:08:32 | 03:10:06 | **passed** 03:11:54 | `clear_logs.yml` **succeeded in 23.9 s**; restore 03:17:41, done 03:17:50 |

How to read it honestly:
- The three **simulate** runs are *not* recoveries and are not counted as passes. The runner itself reported each as "0 passed, failed or not recovered" (for example `done: 0 passed, failed or not recovered: ['pool']`), because nothing was fixed. They show the alert, the diagnosis, the ML verdict and the playbook that **would** run.
- The three **live** runs each passed the full chain: Prometheus alert, engine diagnosis, ML agreement, approved playbook, and an independent recovery check. Auto-remediation was ON only for each run (the console showed ON) and the runner restored it to false at the end of each.
- After the last run: console idle, auto-remediation **OFF (safe)**, no alerts firing, the dashboard showed **RECOVERED (VERIFIED)**, all four core deployments ready 1/1. Grafana's engine panels read 19 incidents received (9 escalated, mostly the lone-symptom first alerts the 0.75 gate deliberately holds back), root causes pool 4, memory 4, disk 2, across everything run since the lab came back.
- This is one pass per live scenario in this session, on top of the earlier counted runs (pool 5/5, disk 4/4, memory 3 of 4 acted); it adds the evidence that the **buttons** drive the same pipeline, not a new rate.
- Screenshots: `100` to `114` (simulate), `120` to `128` (live), `129` and `130` (final safe state).

### The Jenkins pipeline, built (2026-11-18 03:24 UTC, build #1)

The owner signed in and the job `autosre-cicd` was built from the branch (commit `8233fce`). **Result: SUCCESS in about 9 min 26 s.**

| Stage | Time | What it did |
|---|---|---|
| Checkout | 1 s | fetched the repository at the exact commit |
| Preflight | 1 s | Minikube running, **no fault scenario running, `AUTO_REMEDIATE` is `false`** (it would have refused otherwise) |
| Unit tests | 8 min 22 s | created a virtual environment, installed the requirements, ran the suite: **no failures** (Jenkins "Latest Test Result: no failures") |
| Build images | 21 s | rebuilt the seven images into Minikube |
| Deploy | 6 s | applied the manifests, **forced the safe defaults**, restarted the seven deployments |
| Verify | 22 s | every deployment rolled out, `AUTO_REMEDIATE` still false, the smoke test traversed frontend, gateway, transaction service and database |

Afterwards all 8 deployments were 1/1 and the fault console still showed auto-remediation **OFF (safe)**. Screenshots `141` (Stage View), `142` (console output ending `Finished: SUCCESS`), `143` (console after the deploy).
Note the unit-test stage is the slow one on the 2-vCPU instance (8 min against about 2.5 min on a laptop); the pipeline's 40-minute limit leaves room.

Two weaknesses found while doing this, neither a failure of a run:
- **The security group for the demo ports was open to 0.0.0.0/0** (found in the AWS console, screenshot `94`), although the guide says to allow only your own IP. Jenkins and the console both carry the throwaway password `password` and can change the cluster; restrict the rules to one IP or delete them. This is a network setting, so it was left for the owner to change.
- **Jenkins**: it signed the browser out (screenshot `140`) and the owner signed in; the pipeline was then built (next section).

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
| coll1, coll2 | dry, `--collect` | real-fault trial data for the scorer experiment (10 and 8 faults); coll2 ran the retrained model: 8/8 agree, `pair_gate` passed |
| **cloud2** | live | pool and disk with the new dispatcher history volume: 2 passed; the restore restarted the dispatcher and the new pod kept both records and the disk cooldown (95 s left of 600 s); `AUTO_REMEDIATE=false` confirmed afterwards, 8 of 8 deployments 1/1 |
| **console-sim** (pool, memory, disk) | simulate, via the fault console | three dry runs: alert, diagnosis, ML agree, playbook chosen, **nothing executed, not counted as recovery** (see "The fault console, run end to end") |
| **jenkins #1** | CI/CD pipeline | **SUCCESS**, about 9 min 26 s: preflight, tests (no failures), build, deploy, verify; safe defaults held |
| **console-live** (pool, memory, disk) | live, via the fault console | **3 of 3 passed** (29.2 s, 24.4 s, 23.9 s); safe defaults restored and confirmed afterwards |
| **cloud1** | live, cold start | pool, memory, disk live and unknown cause: **5 of 5 passed**, guardrails 7/7, default model, restore confirmed (see "The cloud side") |

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
| 8 | Grafana displays the incident and recovery evidence | **Yes**: the deployed dashboard and front end were rendered and looked at; defects found there were fixed, and the front end's incident choice is now deployed too; the same pages were opened again from the user's own browser over the instance address |
| 9 | Unknown and unsafe incidents escalate to a human | Yes: unknown-cause scenario, guardrails, cooldown, two-fault ambiguity, gate veto |
| 10 | Automated tests pass; deployment documented | Yes (about 305 tests, 288 passed in the last full run before the 17 fault-console tests were added; `01-audit.md`, `02-deploy-demo-cleanup-rollback.md`, this report) |

## Limits, and what was done about each

| Limit (from the previous version of this report) | What was done | Status |
|---|---|---|
| Memory's slowness was injected; a scale-out leaves the leak | The slowness is now real work on the leaked heap (no sleep), calibrated by measuring. The "alert persists" assumption was wrong: the alert sums across pods and resolves, and recovery now requires that plus 24 live transactions with no errors and a median under 0.3 s. The leak itself stays in the original pod by design (the approved playbook is a scale-out); the runner replaces that pod after each trial | **Verified live** (comp12, comp13): 3 of 4 acted and recovered. **Still true:** a scale-out does not cure a leak, a human must replace the pod |
| One passing run per scenario, not a rate | pool 5/5, disk 4/4, memory 3 acted of 4 (1 escalated by the ML gate), cooldown, unknown cause and live guardrails each repeated against the real fault models | **Counted, still small.** These are not confidence intervals. Memory acted on 3 of 4 in the repeat runs and the scorer then agreed on 5 of 5 more in the dry-run batch; 8 of 9 in all. Too few to call a rate either way |
| Front end and Grafana pages not viewed | Both rendered in a real browser from the deployed cluster; four defects found and fixed | **Done**, including the last front-end fix |
| memory, cooldown, guardrails, unknown cause not repeated against the new fault models | All four were run against them (comp13, comp14) | **Done** |

New things this work found and did **not** fix:
- The one scorer disagreement on a real memory fault (above). Retraining on real-fault trials was tried and changed nothing measurable; the open question is whether the engine's estimated `fault_start` window differs from the true-start windows I recorded.
- The fault console is now proven end to end (three simulate and three live runs, above). The Jenkins `autosre-cicd` job has now been built once, successfully (build #1, above).
- The fault console's status call can hang when the cluster is busy (it runs `kubectl` for every poll), which made the page show "console unreachable".
- The dispatcher's cumulative counters (not the execution list or cooldowns) still reset on a restart.
- Lab caveats: the instance runs a single-node Minikube on 2 vCPUs, so timings are not production numbers, and every fault is started by the runner.

Cluster state at the end: `AUTO_REMEDIATE=false`, `MIN_CONFIDENCE=0.75`, 8 of 8 deployments 1/1, no runner process. The lab instance and the S3 bucket were left in place, as agreed.
