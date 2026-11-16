# Test report: what was run on the real cluster, what it showed, what is still open

Branch `devops-completion`, last commit before this report `f143fdb`. Cluster: Minikube (containerd) on the AWS Academy lab EC2 instance
(`i-04f8f4d75378a1aba`), isolated `autosre` namespace, 2026-11-16. Raw results: `s3://autosre-validation-841736140404/results-comp3/` to
`results-comp6/` (`<run>.log`, `report.md`, `scenarios.json`). Unit tests: **254 passed** (`pytest tests correlation-engine/tests second-opinion/tests`);
baseline on `devops-integration` was 150. The unit tests use a fake cluster and a stubbed `ansible-playbook`; they do not show that a playbook
works. Only the runs below do.

**Nothing here is simulated recovery.** Each scenario says what kind of fault it is. Pool exhaustion injects the pool gauge (no real connections are held);
memory holds real memory in the container; disk writes real bytes into the database pod's bounded log directory.

## Runs

| Run | Mode | What it showed |
|---|---|---|
| comp1, comp2 | dry | Stopped at preflight, by design: the second-opinion manifest pinned an image tag nothing builds (`ErrImageNeverPull`); then the runner's JSON POST was rejected (HTTP 422) |
| comp3 | dry | Chain reached the dispatcher for pool, memory, disk, but every dispatcher answer was "Confidence below threshold"; the unknown-cause scenario saw **no alert** because the load generator produced no backend traffic |
| comp4 | dry | First full dry run with the fixes (table below) |
| comp5 | **live** | Pool reached the dispatcher (accepted, `restart_service.yml`) and the **dispatcher's preflight stopped it safely**: its Kubernetes client was `system:anonymous` (403). No change was made to the cluster |
| comp6 | **live** | pool and cooldown passed end to end; disk ran `clear_logs.yml` and failed on RBAC (table below) |
| comp7 | live (cancelled) | Cancelled before any fault, because the lab had about 11 minutes left; safe defaults forced and confirmed |

### comp4, dry run (decisions only; nothing was executed)

| Scenario | Fault kind | Outcome | Alert after | Engine incident after | Cause named | Confidence | ML verdict |
|---|---|---|---:|---:|---|---:|---|
| guardrails (7 checks) | direct API checks | passed 7/7 | | | | | |
| pool | injected gauge + injected errors | dry_run: every check passed | 56 s | 87 s | db_connection_pool_exhaustion | 0.75 | agree |
| disk | real bytes + injected errors | dry_run: every check passed | 82 s | 112 s | db_disk_full | 0.85 | agree |
| unknown cause | injected errors, no deciding symptom | passed: escalated, nothing executed | 91 s | n/a | none | n/a | n/a |
| memory | real memory + 0.15 s latency | escalated by policy | 76 s | 560 s | high_memory_usage | 0.60 | **disagree** |

### comp6, live run (AUTO_REMEDIATE on for the run only; restored and confirmed off afterwards)

| Scenario | Outcome | Alert after | Incident after | Cause | Confidence | ML | Playbook | Recovered |
|---|---|---:|---:|---|---:|---|---|---|
| guardrails_dry | passed 7/7 | | | | | | | |
| **pool** | **passed**: alert, diagnosis, second opinion, playbook and independent recovery all confirmed | 56 s | 118 s | db_connection_pool_exhaustion | 0.75 | agree | `restart_service.yml` | **yes** (alert resolved, deployment ready, `/health` ok) |
| **cooldown** | **passed**: same fault again was `skipped/cooldown`; executions 1 to 1 | | | db_connection_pool_exhaustion | 0.75 | agree | none started | n/a |
| disk | **failed**: `clear_logs.yml` started and ansible exited 2 | 87 s | 113 s | db_disk_full | 0.85 | agree | `clear_logs.yml` | no |
| guardrails_live | passed 6/6, no execution started | | | | | | | |

Disk failure, exactly: the dispatcher's service account was forbidden to open exec on the database pod (`pods/exec`, HTTP 403). The Python client opens
exec as a WebSocket, which needs the `get` verb as well as `create`; the Role granted only `create`. Fixed in `f143fdb` (`get` and `create`).
**Not yet re-run on the cluster**, so the disk playbook is **not** verified end to end.

## Bugs that only the real cluster showed (all fixed and covered by a test)

1. `k8s/second-opinion.yaml` pinned a tag no script builds, with `imagePullPolicy: Never`: a clean deploy could never start the scorer.
2. The dispatcher image left `/tmp/.ansible` root-owned, so `ansible-playbook` failed as the non-root user.
3. The load generator fetched the frontend's static page, so no traffic reached the gateway, transaction service or database; `ErrorRateHigh` and `APILatencyHigh` could never fire.
4. `kubectl create --raw` sends no JSON content type, so the chaos endpoints answered 422 to the runner.
5. A lone deciding symptom scores 0.55 to 0.60, below the 0.75 gate, so the first webhook of every incident is rightly escalated. The scenarios now also inject the consequence a real fault causes (errors, latency) and the runner judges the incident the dispatcher acts on.
6. The `kubernetes` Python package 36.0.0 sent requests as `system:anonymous`; the dispatcher now talks to the API server by REST with its service-account token, and the version used by Ansible is pinned to 31.0.0.
7. `pods/exec` needs `get` as well as `create` (the disk failure above).
8. A relaunch killed the previous runner without running its restore, **leaving `AUTO_REMEDIATE=true`** on the dispatcher. It was found, switched off, and verified. The runner now restores on SIGTERM; the launcher waits for the old runner and forces the safe defaults before and after `kubectl apply`.
9. The transaction service hard-coded a pool maximum of 10 while its manifest said 20.
10. My image check used `docker run` against a containerd cluster; it now runs inside the pod. Playbook syntax and the collection (`kubernetes.core 6.6.0`, `ansible-core 2.21.6`) were verified there.

## Acceptance criteria, honestly

| # | Criterion | Status |
|---|---|---|
| 1 | All three faults can be injected | Yes: pool (injected gauge), memory (real), disk (real bytes) |
| 2 | Prometheus detects the correct symptoms | Yes: 56 to 87 s |
| 3 | The engine identifies the correct root cause | Yes, all three (memory at confidence 0.60) |
| 4 | The ML second opinion produces a recorded verdict | Yes: agree, agree, and **disagree** for memory |
| 5 | The dispatcher selects the correct approved playbook | Pool and disk yes; memory never reached (below the gate) |
| 6 | Ansible executes the intended operation | **Restart yes (live).** Clear logs ran but failed on RBAC (fixed, not re-run). Scale-out **not run live** |
| 7 | Kubernetes and application health confirm recovery | **Pool yes.** Disk and memory not yet |
| 8 | Grafana displays the incident and recovery evidence | Not verified: the dashboard imported, its panels were not inspected |
| 9 | Unknown and unsafe incidents escalate to a human | Yes: unknown-cause scenario; 7/7 dry and 6/6 live guardrail checks; cooldown |
| 10 | Automated tests pass; deployment documented | Yes (254 tests; `01-audit.md`, `02-deploy-demo-cleanup-rollback.md`) |

**The project is not complete by the stated criteria.** One scenario is verified end to end (pool). Disk needs one more live run after the RBAC fix. Memory is blocked
by policy (see below) and its scale-out has never run live.

## Still open

- **Disk, live**: re-run `guardrails_dry,disk` with `LIVE=1` (the lab had too little time left; comp7 was cancelled safely).
- **Memory**: confidence is 0.60 from the deciding symptom alone and the ML second opinion **disagreed** (comp4), so the default policy (`SECOND_OPINION_POLICY=agree`, threshold 0.75) correctly refuses to act. The latency consequence was raised to 0.3 s (`7c5ce2b`) and has not been re-run. If the second opinion keeps disagreeing, scale-out stays escalated by design.
- **ML validation (Phase 3) on the cluster**: the two-fault scenarios `pair_ambiguity` and `pair_gate` (decisions only) have not been run on this branch. Earlier evidence is in `docs/evaluation.md` and `docs/real-aws-validation/`.
- **Front end and Grafana**: tested by unit tests only; not looked at on the cluster.
- Live execution was approved once, for pool, disk, cooldown and guardrails only (not memory). Any further live run needs new approval.
