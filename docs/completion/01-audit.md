# AutoSRE completion: repository audit and implementation plan

Branch audited: `devops-integration` at `f15986a`. Work happens on `devops-completion` (local worktree, new branch). Baseline before any change:
`pytest correlation-engine/tests tests second-opinion/tests` gives **150 passed**. The remediation dispatcher has **no tests**.

Method: every file named in the plan was read in full; findings below are from the code, not from the plan's status table.
Severity: **S1** blocks the acceptance criteria, **S2** produces a wrong or unsafe result, **S3** is a gap or inconsistency.

## What exists and works

Four FastAPI services with Prometheus metrics and chaos endpoints; six alert rules; Alertmanager routing to the engine; the correlation engine
(rules, signatures, ambiguity rule, shadow/gate second opinion); the second-opinion scorer and weights; a Grafana dashboard with 13 panels;
a dispatcher with confidence threshold and an allow-list; `restart_service.yml` and `scale_replicas.yml`. The engine already posts each
correlated incident to the dispatcher (`DISPATCHER_ENABLED=true` in the manifest).

## Findings

### Dispatcher (`remediation-dispatcher/app.py`) and its image

| ID | Sev | Finding |
|---|---|---|
| D1 | S1 | `ALLOWED_PLAYBOOKS` lacks `clear_logs.yml`, so `db_disk_full` always escalates ("not approved"). |
| D2 | S1 | **Timeout mismatch.** `/dispatch` is synchronous and runs ansible for up to 180 s (restart waits up to 150 s); the engine's `DISPATCHER_TIMEOUT` is 5 s. The engine records "unavailable" while the playbook keeps running, and the engine's repeat webhook can start a second run. |
| D3 | S1 | **No cooldown, no duplicate protection, no execution tracking.** The front end advertises "Cooldown ENGAGED (10m)" but nothing implements it. |
| D4 | S1 | **The second opinion is not used by the dispatcher.** The engine sends only `{root_cause, confidence}`; the dispatcher cannot require agreement, so "model agreement requirements" cannot be enforced. The service is also not sent. |
| D5 | S1 | **Image cannot be built from a clean clone.** The Dockerfile does `COPY remediation-dispatcher/collections/`, which is git-ignored, and expects the repository root as the build context. `scripts/build-images.sh` does not build the dispatcher at all, and `deploy.sh` does not wait for it. |
| D6 | S2 | Success is `ansible rc == 0`. There is no independent check that the rollout finished, replicas are ready or the application is healthy. No rollback. |
| D7 | S2 | `target_service`, namespace and `replicas` are taken from the map file with no validation; nothing stops a bad map entry from touching another deployment or scaling to a huge number. |
| D8 | S2 | RBAC Role allows deployments get/list/patch/update and scale, but **not** pods get/list or `pods/exec` (needed by `clear_logs.yml`), so the disk playbook cannot work as written. |
| D9 | S3 | No custom metrics (`remediation_*`); the dispatcher cannot be charted or alerted on. `/health` is used for both probes. `kubernetes` is in requirements but never used. |

### Ansible

| ID | Sev | Finding |
|---|---|---|
| A1 | S1 | `clear_logs.yml` is a placeholder: the delete task is commented out and it prints "NOT IMPLEMENTED". |
| A2 | S2 | `restart_service.yml` waits until `readyReplicas == spec.replicas`, which is already true for the old pods **before** the rollout starts, so it can report success without waiting. It should compare `observedGeneration`, `updatedReplicas` and `availableReplicas`. |
| A3 | S3 | Inconsistent variable names (`k8s_namespace` vs `namespace`); no assertion of allowed service or namespace inside the playbooks; `scale_replicas.yml` does not check the range of `replicas` or record the previous count. |

### Fault injection and application

| ID | Sev | Finding |
|---|---|---|
| F1 | S1 | **Disk fault is a number, not a disk.** `/chaos/disk` sets a stored gauge and nothing is on disk, so `clear_logs.yml` has nothing to delete and recovery can only be simulated. The database has no log volume (`k8s/database.yaml` has no volume). Real recovery needs a real, bounded directory. |
| F2 | S2 | **Pool fault is a number too.** `/chaos/pool` sets the gauge only; the pool maximum is 10 here, while the front end and earlier docs say 20. After a restart the gauge returns to 2, so the restart "fixes" it by resetting a number. This is acceptable as an injection but must be labelled injected, not real. |
| F3 | S2 | The memory fault (`/chaos/memory`) allocates real memory (good). After the scale-out to 3 replicas the extra replicas do not hold the leak, but the original pod still does; recovery must be judged on the alert clearing and the deployment being healthy, not on memory vanishing. |
| F4 | S3 | `scripts/build-images.sh` names six images and the directory `services/transaction_service` (underscore) while the manifests use `transaction-service`; fine today, fragile. |

### Front end and monitoring

| ID | Sev | Finding |
|---|---|---|
| U1 | S1 | The front end's engine and dispatcher panels are **static placeholders** ("awaiting_telemetry", "ENGAGED (10m)"). No incident, root cause, confidence, verdict, playbook, execution or recovery is shown. Pool "2 / 20" and "≥ 18" are hard-coded and do not match the real maximum (10). |
| U2 | S2 | Neither the engine nor the dispatcher exposes recent incidents/executions as JSON, so the front end has nothing to read. |
| U3 | S3 | The Grafana dashboard has no remediation panels (no `remediation_*` metrics exist yet). |

### ML / engine

| ID | Sev | Finding |
|---|---|---|
| M1 | S2 | The engine dispatches on every correlated incident, including repeat webhooks for the same incident, and in shadow mode regardless of the verdict. Safe only because the dispatcher is in dry-run; it must carry the verdict and rely on the dispatcher's policy once execution is enabled. |
| M2 | S3 | The ML evidence for the three causes under the gate, ambiguity and unknown cases exists (docs/evaluation.md, real-AWS summary) but has never been run against **this** branch's dispatcher path. |

## Prioritised plan (this branch)

| # | Phase | Work | Needs a cluster? |
|---|---|---|---|
| 1 | Dispatcher and Ansible | D1-D9, A1-A3: allow-list, validation, cooldown and duplicate guard, async execution with status, verification, rollback for scale, metrics, RBAC, image build; engine passes service and verdict; complete `clear_logs.yml`; tests | Unit tests locally; playbooks and image on the AWS instance |
| 2 | Fault scenarios | F1: real bounded disk (log directory on an `emptyDir`); keep pool/memory; one scenario runner that records Fault, Prometheus, alert, webhook, cause, verdict, playbook, recovery | AWS (Minikube there) |
| 3 | ML validation | M1-M2: threshold 0.75, disagreement, gate, unknown, ambiguous, simultaneous; metrics report | AWS |
| 4 | Front end and Grafana | U1-U3: engine `/incidents`, dispatcher `/executions`, real panels; remediation panels | Unit tests locally; look on AWS |
| 5 | Integration | regression, new integration tests, cooldown and duplicate, safe failure, all deployments Ready, evidence and final report | AWS |

## Environment and safety decisions

- Local PC has Python only (no Docker, Minikube, kubectl, Helm, Ansible, WSL). Code and unit tests run locally; every cluster step runs on the AWS Academy lab instance (Minikube + Docker), after you sign in to the lab.
- `AUTO_REMEDIATE=false` stays the default in every manifest. Live execution is switched on only for a named run, with your approval, in the isolated `autosre` namespace of the lab instance, and switched off again after.
- Nothing is pushed to `devops-integration`, `main`, `ml-and-cloud-validation` or `ml-cloud-additions`. Results go to a new branch `devops-completion`.
- A scenario is reported as recovered only if Kubernetes and the application health checks confirm it; injected faults are labelled injected.
