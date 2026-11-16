# Test report: what was run on the real cluster, what it showed, what is still open

Branch `devops-completion`. Cluster: Minikube (containerd) on the AWS Academy lab EC2 instance (`i-04f8f4d75378a1aba`), isolated `autosre` namespace,
2026-11-16 and 2026-11-17. Raw results: `s3://autosre-validation-841736140404/results-comp3/` to `results-comp10/` (`<run>.log`, `report.md`, `scenarios.json`).
Unit tests: **255 passed** (`pytest tests correlation-engine/tests second-opinion/tests`); the baseline on `devops-integration` was 150. The unit tests use a fake
cluster and a stubbed `ansible-playbook`; they do not show that a playbook works. Only the runs below do.

**Nothing here is simulated recovery.** Each scenario says what kind of fault it is: pool exhaustion sets the pool gauge (no real connections are held) and
injects the transaction errors an exhausted pool would cause; memory holds real memory in the container and injects the slowness that causes; disk writes real
bytes into the database pod's bounded log directory and injects the write errors a full disk causes. Those consequence symptoms are injected because a lone
deciding symptom scores only 0.55 to 0.60, below the dispatcher's 0.75 gate, which then (correctly) refuses to act.

## Live results (AUTO_REMEDIATE on for the run only; restored and confirmed off afterwards)

| Scenario | Run | Outcome | Alert after | Incident after | Cause | Confidence | ML | Playbook | Recovered (independent check) |
|---|---|---|---:|---:|---|---:|---|---|---|
| pool | comp6 | **passed** | 56 s | 118 s | db_connection_pool_exhaustion | 0.75 | agree | `restart_service.yml` | yes |
| pool | comp9 | **passed** (reproduced) | 51 s | 128 s | db_connection_pool_exhaustion | 0.75 | agree | `restart_service.yml` | yes |
| cooldown | comp6, comp9 | **passed**: the same fault again was `skipped/cooldown`; executions 1 to 1 | | | db_connection_pool_exhaustion | 0.75 | agree | none started | n/a |
| **memory** | comp10 | **passed** | 56 s | 128 s | high_memory_usage | 0.75 | agree | `scale_replicas.yml` | yes |
| **disk** | comp10 | **passed** | 66 s | 82 s | db_disk_full | 0.75 | agree | `clear_logs.yml` | yes (disk ratio back to 0.30) |
| unknown cause | comp4, comp10 | **passed**: escalated to a human, nothing executed | 91 s | n/a | none | n/a | n/a | none | n/a |
| guardrails (7 checks dry, 6 live) | comp4, comp6, comp8 to comp10 | **passed** every time, no execution started | | | | | | | |

"Independent check" means the runner, not the dispatcher, confirmed recovery: the deployment is fully ready, `/health` is ok, and the alert resolved (pool, disk),
or three replicas are ready (memory; a scale-out does not remove the leak in the original pod, so that alert is expected to persist). The dispatcher also
verifies each run itself (rollout, `/health`, disk metric) before it reports `succeeded`.

### Dry-run and Phase 3 (ML) results, decisions only

| Scenario | Run | Result |
|---|---|---|
| pool / disk | comp4 | `dry_run`: every dispatcher check passed (confidence 0.75 and 0.85, ML agree); nothing executed |
| memory (0.15 s, 0.3 s latency) | comp4, comp8 | escalated by policy: confidence stayed at 0.60. The second opinion **disagreed** in comp4 and agreed in comp8 |
| **two faults, ambiguity rule on** | comp8 | **passed**: memory then pool escalated with "deciding symptoms of 2 causes fired together"; nothing executed |
| **two faults, gate mode on** | comp8 | **passed (recorded)**: the incident was **vetoed**, the second opinion disagreed with the dominant fault; nothing executed |

Together with the earlier evidence (`docs/evaluation.md`, `docs/real-aws-validation/`): the 0.75 threshold holds (a lone deciding symptom never acts), a disagreeing or
missing second opinion blocks execution, two simultaneous causes escalate when the ambiguity rule is on, and the gate vetoes when the scorer disagrees.

### Front end and Grafana, checked on the cluster

After the live runs, the front end's `/api/state` returned real data: both executions (`clear_logs.yml` succeeded in 26.5 s, `scale_replicas.yml` in 25.5 s, `recovered: true`),
all four deployments 1/1, no firing alerts, pool 2 / 20, disk 0.3, and the engine's recent incidents. Prometheus returned the `remediation_executions_total` and
`remediation_decisions_total` series that the six new Grafana panels chart. **Not done:** I did not look at the rendered web page or the Grafana panels
in a browser, so the visual layout is untested.

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

## Acceptance criteria, honestly

| # | Criterion | Status |
|---|---|---|
| 1 | All three faults can be injected | Yes: pool (injected gauge), memory (real), disk (real bytes) |
| 2 | Prometheus detects the correct symptoms | Yes: 51 to 91 s |
| 3 | The engine identifies the correct root cause | Yes, all three, at confidence 0.75 to 0.85 |
| 4 | The ML second opinion produces a recorded verdict | Yes, on every incident (agree on all passing live runs) |
| 5 | The dispatcher selects the correct approved playbook | Yes: restart, scale, clear logs |
| 6 | Ansible executes the intended operation | **Yes, all three, live** |
| 7 | Kubernetes and application health confirm recovery | **Yes, all three**, by an independent check (memory = capacity ready and healthy) |
| 8 | Grafana displays the incident and recovery evidence | **Partly**: the data the panels use is present and the front end serves it; the rendered pages were not viewed |
| 9 | Unknown and unsafe incidents escalate to a human | Yes: unknown-cause scenario, guardrails, cooldown, two-fault ambiguity, gate veto |
| 10 | Automated tests pass; deployment documented | Yes (255 tests; `01-audit.md`, `02-deploy-demo-cleanup-rollback.md`, this report) |

## Limits and what is still open

- **Sample size.** Each live scenario was run once to a pass (pool and cooldown twice). That shows the chain works; it is not a reliability rate.
- **The pool fault is not a real exhausted pool**, and each scenario's consequence symptom is injected (see the top). The memory scale-out leaves the original pod's leak in place.
- **Not viewed in a browser:** the rendered front end and the Grafana dashboard.
- **Memory depends on the second opinion agreeing.** It disagreed once (comp4) and agreed later (comp8, comp10); with the default policy a disagreement blocks the scale-out, correctly.
- Live execution was approved by the user for all fault scenarios for this session. Any further live run needs new approval. The cluster is left with `AUTO_REMEDIATE=false`.
