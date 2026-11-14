# Actual automatic self-healing on local Minikube

This is an opt-in development demonstration restricted to
`autosre/transaction-service`, root cause `db_connection_pool_exhaustion`, and
`restart_service.yml`. ML remains in shadow mode. Disk cleanup and scaling are
not executable through this dispatcher. No namespace, database, or volume is
deleted. The ordinary base manifests still default to dry-run and disabled
fault injection.

## Verified result

On November 12, 2026, a bounded in-memory pool reservation fault made actual
`POST /transaction` requests fail with HTTP 503. This is a resource-fault model
in the mock transaction application, not a real external database outage. No
synthetic Alertmanager payload was submitted.

The real pipeline observed the application metrics, fired `DBPoolNearExhaustion`
and `ErrorRateHigh`, delivered Alertmanager webhooks, diagnosed pool exhaustion
at 0.75 confidence, obtained the own-data model's second opinion, and accepted
job `e99510a7-f501-46ea-ba59-b2edc6520de0`. The model disagreed (it classified the
metrics as high memory); shadow mode correctly continued. A preceding pool-only
notification had confidence 0.55 and was escalated without a restart.

The dispatcher executed Ansible successfully, automatically advanced Deployment
generation 8 to 9, and replaced pod `transaction-service-7f457dcf64-mg9jb` with
`transaction-service-68677c54b8-xrf7t`. It verified the new generation, removal of
old replicas, a new application instance, pool utilization 2/20 (10%), and a
successful transaction before recording success. The traffic driver counted
411 failing transactions followed by HTTP 200. No manual restart or emergency
pool reset was used to recover the fault.

A repeated dispatch returned `cooldown` for the same job with 577 seconds
remaining and did not advance the Deployment generation. Prometheus scraped
`remediation_success_total=1`, `remediation_failure_total=0`, and
`remediation_cooldown_total=1`. See
[self-healing-evidence.json](self-healing-evidence.json) for the durable evidence
summary. After proving recovery, the pool hook was disabled; that configuration
change produced a separate cleanup rollout, not the recovery rollout above.

## Implementation and safeguards

- Only the fixed root cause, namespace, service, playbook, and mapping are
  accepted. Non-finite/out-of-range/low confidence is rejected. The live pool
  must actually be exhausted before a restart is attempted.
- RBAC permits only `get` and `patch` on the named transaction-service
  Deployment. It does not permit scaling, deleting pods, or modifying another
  Deployment. The playbook independently asserts the same scope.
- Kubernetes client 31.0.0 is pinned. An unbounded newer client observed during
  implementation had incompatible authentication field names and sent an
  anonymous request. Both Python and Ansible authenticated reads were verified
  with the mounted ServiceAccount, TLS verification, and restricted RBAC.
- The image includes ansible-core 2.21.6, vendored kubernetes.core 6.6.0,
  jsonpatch, Kubernetes Python dependencies, and an explicit default stdout
  callback. It does not require an external kubeconfig or SSH access.
- `POST /dispatch` reserves work atomically and returns HTTP 202 with a job ID.
  An in-process background worker performs the action, avoiding the engine's
  five-second HTTP timeout. HTTP 202 means accepted, not recovered. Poll
  `GET /remediations/{job_id}` for `succeeded`, `not_needed`, or `failed`.
- A SQLite database at `/data/remediation.sqlite3`, on PVC `remediation-state`,
  stores the 600-second reservation for each `(root_cause, service)` pair.
  Concurrent and repeated requests cannot restart the same target twice.
  The dispatcher uses one replica and a Recreate strategy for its SQLite/PVC.
- Ansible stamps a unique operation ID, waits for `observedGeneration`, and
  requires updated, total, Ready, and available replica counts to equal the
  desired count. Already-Ready old pods do not satisfy a new rollout.
- The dispatcher separately verifies rollout identity, new instance identity,
  a recovered pool, and a successful transaction. Failures are durably recorded
  with `action=escalate_to_human` and block further automatic attempts even
  after cooldown expiry. A lost worker also leaves an unresolved reservation;
  after expiry it requires operator review rather than silently retrying.
- General `CHAOS_ENABLED` remains false. Only `POOL_FAULT_ENABLED` temporarily
  enables the bounded pool endpoint. Memory, latency, error injection, and disk
  hooks are not enabled for this demonstration.

## Images and deployment

Images are local to Minikube and are not pushed to a registry:

| Image | Manifest SHA-256 |
|---|---|
| `autosre/remediation-dispatcher:self-healing-v2` | `90074bb4c61e282841c39da88cb76eb7de5d0d1f412a2a7b4d486be5250f609c` |
| `autosre/transaction-service:self-healing-v1` | `b38339f14833ba0e7363d37224c29868ff25848fe7851c388789ab08adf206dc` |

The dispatcher v1 build was replaced by v2 to fix the discovered client
authentication incompatibility. Only dispatcher and transaction images were
rebuilt; the existing engine, scorer, database, frontend, and gateway images
were retained. Before deployment on another node/environment, build or load the
reviewed images there. Pull policy `Never` will not fetch them remotely.

From the repository root, after reviewing the source and local context:

```powershell
$mk = 'C:\minikube\minikube.exe' # Or your installed minikube executable.
& $mk -p minikube image build -t autosre/remediation-dispatcher:self-healing-v2 `
    -f remediation-dispatcher/Dockerfile .
Push-Location services/transaction_service
try {
    & $mk -p minikube image build -t autosre/transaction-service:self-healing-v1 `
        -f Dockerfile .
} finally { Pop-Location }
```

The dispatcher needs repository-root context because it copies `ansible/` as
well as its own source and collection. During the verified build, these exact
inputs were copied to a small temporary context to avoid sending virtual
environments. A future build may resolve different unpinned transitive packages;
verify syntax, dependencies, source, and image identity before deploying.

## Repeat the real demonstration

These commands intentionally affect only the controlled local deployment. They
are not a production procedure. Confirm context `minikube`, namespace `autosre`,
all pods Ready, working monitoring, and no existing real AutoSRE incident first.
Wait at least ten minutes after any prior successful attempt and until the
prior fault's alert window has cleared. Do not erase cooldown history to repeat
the demonstration sooner.

If deploying the implementation for the first time (images must already exist):

```powershell
kubectl --context=minikube apply -f k8s/remediation-dispatcher.yaml -f k8s/transaction-service.yaml
kubectl --context=minikube -n autosre rollout status deployment/remediation-dispatcher --timeout=180s
kubectl --context=minikube -n autosre rollout status deployment/transaction-service --timeout=180s
```

Arm the restricted action and pool-only hook, run the demonstration, and always
close the injection window afterward. Use a new output directory on every run:

```powershell
kubectl --context=minikube -n autosre patch deployment remediation-dispatcher `
    --type=strategic --patch-file k8s/self-healing-demo/dispatcher-patch.yaml
kubectl --context=minikube -n autosre rollout status deployment/remediation-dispatcher --timeout=180s
kubectl --context=minikube -n autosre patch deployment transaction-service `
    --type=strategic --patch-file k8s/self-healing-demo/transaction-patch.yaml
kubectl --context=minikube -n autosre rollout status deployment/transaction-service --timeout=180s
try {
    .\.venv312\Scripts\python.exe -B scripts/demo-self-healing.py `
        --context minikube --out "$env:TEMP\autosre-selfheal-demo-$(Get-Date -Format yyyyMMddHHmmss)"
    if ($LASTEXITCODE -ne 0) { throw 'Inspect the demonstration evidence before continuing' }
} finally {
    kubectl --context=minikube -n autosre set env deployment/transaction-service POOL_FAULT_ENABLED=false
    kubectl --context=minikube -n autosre rollout status deployment/transaction-service --timeout=180s
}
```

The runner injects pool capacity through `/chaos/pool`, drives real requests
until a successful recovered transaction, observes real firing alerts, reads
the completed job, checks cooldown, and saves logs and snapshots. On failure it
attempts an emergency application-state reset through the bounded pool hook;
it never substitutes a manual restart for automatic recovery. Such a reset
would be recorded explicitly and would not count as a successful automatic test.

Afterward verify the pool is healthy, the expected alerts have resolved, all
pods/targets are Ready/UP, and no unrelated Deployment changed. Error-rate
alerts use a five-minute rate window and can remain firing after requests have
recovered; do not change or silence production escalation to hide this delay.
`InfoInhibitor` is self-inhibited monitoring bookkeeping and is not an incident.

## Logs, metrics, and operator escalation

```powershell
kubectl --context=minikube -n autosre logs deployment/remediation-dispatcher --since=15m
kubectl --context=minikube -n autosre logs deployment/correlation-engine --since=15m
kubectl --context=minikube -n autosre logs deployment/second-opinion --since=15m
kubectl --context=minikube -n autosre get pods,deployments,pvc
```

Query Prometheus using `job` for the exporter and `exported_service` for the
application label that conflicts with Kubernetes' Service label:

```promql
remediation_success_total{namespace="autosre",job="remediation-dispatcher",exported_service="transaction-service"}
remediation_failure_total{namespace="autosre",job="remediation-dispatcher",exported_service="transaction-service"}
remediation_cooldown_total{namespace="autosre",job="remediation-dispatcher",exported_service="transaction-service"}
remediation_in_progress{namespace="autosre",job="remediation-dispatcher"}
second_opinion_verdicts_total{namespace="autosre",job="second-opinion",exported_service="transaction-service"}
```

A failed job is an operator escalation, not a retry invitation. Disable execution
while investigating its persisted reason, Ansible output, current Deployment,
and application health. There is deliberately no unauthenticated HTTP endpoint
that clears failure/cooldown history. Do not delete its PVC or database. Review
and consciously resolve an interrupted/failed reservation before rearming.

To return to dry-run after a demo, without removing evidence:

```powershell
kubectl --context=minikube -n autosre set env deployment/remediation-dispatcher AUTO_REMEDIATE=false
kubectl --context=minikube -n autosre rollout status deployment/remediation-dispatcher --timeout=180s
```

## Tests and limits

The final local run passed 174 tests across dispatcher, pool-fault, chaos-guard,
correlation-engine, and second-opinion suites (two dependency warnings). Tests cover concurrent reservations, persisted
cooldown and expiry, failure/interruption lockout, forbidden targets/actions,
non-finite confidence, healthy-target no-op, stale/surge rollout rejection,
verified recovery, and failure when rollout completes without recovery.

The implementation is deliberately single-replica and single-node: SQLite is
not a distributed worker queue. Accepted work is not automatically replayed
after a crash, and metrics counters reset on process restart while job history
persists. A failed or interrupted job requires operator review. The image and
PVC are local Minikube artifacts, and the scoped test does not validate scale
remediation, disk cleanup, real database failures, ML gate mode, or general
diagnostic accuracy. The ML disagreement is recorded, not represented as model
agreement. The dispatcher endpoint assumes a trusted local development cluster;
an authenticated control plane is needed before exposing it outside that scope.
The existing kube-controller-manager, kube-etcd, and kube-scheduler scrape
targets still refuse connections. All seven AutoSRE targets are UP; those
unrelated infrastructure scrape issues were not changed by this demonstration.
