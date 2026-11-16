# Deploy, demonstrate, clean up and roll back

Applies to branch `devops-completion`. Everything runs in the `autosre` namespace of one Minikube cluster (a laptop, or the AWS lab
instance). Do not run any of this against a shared or production cluster.

## Safe defaults (what the manifests ship with)

| Setting | Default | Where |
|---|---|---|
| `AUTO_REMEDIATE` | `false` (decisions are recorded, nothing is executed) | `k8s/remediation-dispatcher.yaml` |
| `MIN_CONFIDENCE` | `0.75` | same |
| `SECOND_OPINION_POLICY` | `agree` (the scorer must agree before anything runs) | same |
| `COOLDOWN_SECONDS` | `600` per (cause, service) | same |
| `CHAOS_ENABLED` | off on every service | `k8s/*.yaml` |
| `SECOND_OPINION_MODE` | `shadow` (the scorer annotates, never vetoes) | `k8s/correlation-engine.yaml` |
| `ESCALATE_ON_AMBIGUITY` | unset (off) | same |

Live execution is switched on only for a named run, by the scenario runner with `--live`, and the runner switches it off again in a
`finally` block. After any run, confirm: `kubectl -n autosre exec deploy/remediation-dispatcher -- python -c "import urllib.request;print(urllib.request.urlopen('http://localhost:8001/health').read())"` shows `"auto_remediate": false`.

## Deploy

```bash
bash scripts/build-images.sh      # seven images, built inside minikube's Docker (the dispatcher builds from the repository root)
bash scripts/deploy.sh            # namespace, manifests, rollouts, ServiceMonitor and alert rules
bash scripts/install-dashboard.sh # Grafana dashboard (13 original panels plus six remediation panels)
```

Needs kube-prometheus-stack in the `monitoring` namespace (see `docs/setup-wsl2.md`). All seven deployments must be `Ready`
(`kubectl -n autosre get deploy`).

On the AWS lab, from CloudShell after signing in through the lab's own link:

```bash
curl -fsS https://raw.githubusercontent.com/vaishnavi-nss/autosre-devops/devops-completion/scripts/aws/start-completion-run.sh | RUN=comp1 LIVE=0 bash
```

`LIVE=0` records decisions only. `LIVE=1` is a live run and needs your explicit approval.

## Demonstrate (what to show, in order)

1. `kubectl -n autosre get deploy` : seven `Ready`. Dashboard "Rollout and Recovery" panel shows each deployment complete.
2. Run the scenario runner (dry run first). For each of pool, memory, disk it injects the fault and records: the Prometheus alert,
   the Alertmanager alert, the engine incident (cause, confidence, second-opinion verdict, playbook), the dispatcher decision.
3. Live run: the same, and then the dispatcher executes the playbook (`restart_service.yml` / `scale_replicas.yml` / `clear_logs.yml`),
   verifies the rollout, and the runner confirms recovery independently (alert resolved, deployment ready, `/health`, disk ratio).
4. Dashboard: the incident, ML verdict, playbook and execution status on the front end (`minikube service frontend -n autosre`),
   and the remediation panels in Grafana.
5. Guardrails: low confidence, unknown cause, second opinion disagrees or missing, wrong service, malformed input all escalate; a repeat inside
   the cooldown is skipped; none starts an execution.

What each scenario really is: the pool fault sets the pool gauge (injected, no real connections are held); the memory fault holds real memory
in the container; the disk fault writes real bytes into the database's bounded log directory. A memory scale-out does not remove the leak in
the original pod, so the memory alert is expected to persist; "recovered" there means the extra replicas are ready and the service is healthy.

## Clean up

The runner restores everything it changed. To do it by hand:

```bash
kubectl -n autosre set env deploy/remediation-dispatcher AUTO_REMEDIATE=false COOLDOWN_SECONDS- VERIFY_TIMEOUT_SECONDS-
kubectl -n autosre set env deploy/transaction-service CHAOS_ENABLED=false
kubectl -n autosre set env deploy/database CHAOS_ENABLED=false
kubectl -n autosre set env deploy/correlation-engine SECOND_OPINION_MODE=shadow ESCALATE_ON_AMBIGUITY-
kubectl -n autosre scale deploy/transaction-service --replicas=1
```

Injected disk bytes live in an `emptyDir` (bounded to 100Mi) inside the database pod and disappear when the pod is replaced; the approved
cleanup deletes only `*.log` files directly in `/var/log/app`. Do not clean up the lab's EC2 instance, S3 bucket or other AWS resources.

## Roll back

| Problem | Roll back |
|---|---|
| A bad dispatcher or engine image | `kubectl -n autosre rollout undo deploy/<name>` |
| A scale-out that did not verify | the dispatcher scales back to the previous replica count itself (`ROLLBACK_ON_FAILURE`, default on); otherwise `kubectl -n autosre scale deploy/transaction-service --replicas=1` |
| A restart that never completes | the dispatcher records `verification_failed`, escalates, and starts nothing more (cooldown); `kubectl -n autosre rollout undo deploy/transaction-service` |
| Live execution left on | the commands under "Clean up"; the dispatcher also refuses to act when `AUTO_REMEDIATE=false` |
| The whole branch | `git checkout devops-integration` (this work is only on `devops-completion`) |

## Safety behaviour that is tested

Run `python -m pytest tests correlation-engine/tests second-opinion/tests -q`. The dispatcher tests cover: all three playbooks mapped; a
tampered map cannot widen the target, the variables or the replica count; confidence is inclusive at 0.75; the second opinion must agree;
the wrong service is refused; dry run runs nothing; cooldown and duplicate runs are skipped; a failed preflight runs nothing and sets no
cooldown; success needs a verified rollout and `/health`; a failed scale-out is rolled back; the disk playbook is verified against the real
metric; ansible failure and timeout escalate. Those tests use a fake cluster and a stubbed `ansible-playbook`: they prove the dispatcher's
decisions and bookkeeping, **not** that the playbooks work on a real cluster. That is shown only by the live runs in `docs/completion/results/`.
