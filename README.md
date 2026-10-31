# AutoSRE

**AI-assisted root cause analysis and self-healing for banking infrastructure.**

A self-healing observability system that correlates symptoms across dependent banking
services, identifies the true root cause of an incident, and triggers recovery, without a
human manually triaging alerts.

| | |
|---|---|
| **Course** | DevOps team project — Banking & Finance domain |
| **Team** | Skandesh, Shivani, Vaishnavi |
| **Stack** | Docker · Kubernetes · Prometheus · Grafana · Ansible |
| **Full brief** | [`docs/project-brief.md`](docs/project-brief.md) |
| **Setup guide** | [`docs/setup-wsl2.md`](docs/setup-wsl2.md) |

---

## Objective

Build a self-healing observability system that automatically correlates symptoms across
dependent banking services, identifies the true root cause of an incident, and triggers
recovery — without a human manually triaging alerts.

## Problem statement

When a banking service fails, multiple symptom alerts fire simultaneously: CPU high, API
latency high, DB connection failed, pod restarting, error rate high. The real cause is
usually just one of these. Diagnosing it manually costs minutes during a live incident —
minutes a bank cannot afford, since downtime on customer-facing services means failed
transactions.

## Architecture

### Service dependency chain

```
Frontend  ──▶  API Gateway  ──▶  Transaction Service  ──▶  Database
```

Each box is a separately containerised service with its own Kubernetes Deployment and
Service, wired together over cluster DNS
(`transaction-service.autosre.svc.cluster.local`). Requests flow left to right; **failures
cascade right to left**, which is what makes root-cause ranking possible.

### Correlation and recovery flow

```
Prometheus metrics + Kubernetes events + app logs
                    │
                    ▼
            Correlation engine
   (time-window grouping → dependency-graph
    root-cause ranking → symptom-signature
    matching → confidence score)
                    │
                    ▼
            Ranked root cause
     e.g. "db_connection_pool_exhaustion"
                    │
                    ▼
          Automated remediation
   (root-cause label → playbook lookup →
    ansible-playbook run)
```

### Worked example

```
Symptoms fire together:
  APILatencyHigh         (api-gateway)
  ErrorRateHigh          (transaction-service)
  PodRestartsHigh        (transaction-service)
  DBPoolNearExhaustion   (transaction-service)

The engine walks the chain from the database upward, finds the deepest
alerting service, and checks which metric crossed its threshold there:

  ROOT CAUSE: database connection pool exhaustion
  ACTION:     restart_service.yml against transaction-service
```

That incident is stored as a real Alertmanager payload in
[`correlation-engine/tests/fixtures/`](correlation-engine/tests/fixtures/) and is what the
tests run against.

## Correlation algorithm

Rule-based and explainable, not machine learning.

| Step | What happens | Where it lives |
|---|---|---|
| 1 | **Time-window grouping** — alerts arriving within ~30s become one incident | Alertmanager `group_wait`, in `monitoring/kube-prometheus-stack-values.yaml` |
| 2 | **Dependency graph lookup** — map each alert's `service` label to the chain | `correlation-engine/dependency_graph.py` |
| 3 | **Root cause ranking** — the deepest alerting service wins | `correlation-engine/correlate.py` |
| 4 | **Symptom-signature matching** — which named cause fits these alerts | `correlation-engine/signatures.py` |
| 5 | **Confidence score** — weighted share of expected symptoms seen | `correlation-engine/correlate.py` |

> **The `service` label is the contract.** Every rule in `monitoring/alert-rules.yaml` sets
> it, and every alert name there must appear in a signature. Break either and the engine
> cannot place an alert in the chain.

## Root cause → remediation

| Root cause label | Playbook | Action |
|---|---|---|
| `db_connection_pool_exhaustion` | `restart_service.yml` | Restart the affected service |
| `high_memory_usage` | `scale_replicas.yml` | Increase replica count |
| `db_disk_full` | `clear_logs.yml` | Clear old log files |

Unmapped root causes **alert a human** rather than guessing a fix — see
`ansible/remediation_map.yml`.

## Repository layout

```
services/              the four chain services, each a small FastAPI app
  frontend/            → api-gateway
  api-gateway/         → transaction-service, where latency is measured
  transaction-service/ → database, owns the connection-pool gauges
  database/            mock DB, deepest in the chain
correlation-engine/    Alertmanager webhook + the correlation algorithm
  dependency_graph.py  the chain, ordered deepest-first
  signatures.py        symptom signature per root cause
  correlate.py         algorithm steps 2–5
  evaluation/          incident simulator, metrics, evaluator, optional classifier
  tests/               pytest suite
ansible/
  playbooks/           one playbook per root cause
  remediation_map.yml  root cause → playbook
k8s/                   Deployment + Service per component
monitoring/            Helm values, ServiceMonitor, alert rules
scripts/               build, deploy, smoke test
docs/                  project brief and environment setup
```

## Quickstart

Full environment setup is in **[`docs/setup-wsl2.md`](docs/setup-wsl2.md)** — everything
runs inside WSL2 Ubuntu, because Ansible cannot act as a control node on Windows.

```bash
# 1. Cluster
minikube start --driver=docker --cpus=4 --memory=6g

# 2. Monitoring
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm upgrade --install monitoring prometheus-community/kube-prometheus-stack \
  --namespace monitoring --create-namespace \
  -f monitoring/kube-prometheus-stack-values.yaml

# 3. Build and deploy
./scripts/build-images.sh
./scripts/deploy.sh

# 4. Check the chain end to end
./scripts/smoke-test.sh
minikube service frontend -n autosre
```

Run the tests locally without a cluster:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r correlation-engine/requirements-dev.txt
cd correlation-engine && pytest
```

## Build order

Adapted from section 7 of the brief. Owners are for the team to assign.

| # | Step | Status | Owner |
|---|---|---|---|
| 0 | Repo, service skeletons, manifests, playbook stubs | ✅ done | — |
| 1 | **Base app** — build images, deploy to minikube, confirm the chain works | ⬜ | TBD |
| 2 | **Metrics** — install kube-prometheus-stack, confirm scraping, build Grafana dashboards | ⬜ | TBD |
| 3 | **Fault injection** — trigger each symptom, confirm Alertmanager fires | ⬜ | TBD |
| 4 | **Correlation engine** — algorithm steps 2–5 in `correlate.py` | ✅ done, tested on simulated and fixture incidents; ⬜ live validation | ML |
| 5 | **Remediation** — dispatcher that runs the mapped playbook; finish `clear_logs.yml` | ⬜ | TBD |
| 6 | **End-to-end demo** — run the worked example live, show the dip and recovery | ⬜ | TBD |

The engine's accuracy, its failure modes and the decisions the team still has to make are
written up in **[`docs/evaluation.md`](docs/evaluation.md)**. Every number there comes from
**simulated** incidents; the same document has the procedure for capturing real ones.

The one remaining `TODO` marker is in `ansible/playbooks/clear_logs.yml` (step 5).

## Deliverables

- [x] Git repo with working code
- [x] Project title, objective, problem statement
- [x] At least one DevOps tool — this design uses **five** (Docker, Kubernetes, Prometheus,
      Grafana, Ansible), covering the full Build → Deploy → Monitor → Recover loop
- [ ] **Innovation** — event-driven correlation plus remediation, the pattern behind Red
      Hat's Event-Driven Ansible and enterprise AIOps tools (BigPanda, Moogsoft, BMC
      Helix), purpose-built for a banking dependency chain
- [ ] **Society angle** — faster, correct incident recovery reduces failed transactions and
      downtime on customer-facing banking systems

## Open questions for the team

1. **Fail safe or fall back?** When the deepest alerting service matches no known cause,
   the engine escalates to a human instead of acting on a shallower match. That trades
   about 5 points of automation for far fewer wrong playbooks *if* deeper faults occur at
   all — a cost judgement the team should make deliberately. See `docs/evaluation.md`.
2. **Two simultaneous causes.** When two causes' deciding symptoms fire together the engine
   picks one by score, and this is where every wrong action in the evaluation came from.
   Escalating instead is a one-line rule the evaluation recommends but cannot validate.
3. **Confidence gate.** The score measures how complete the symptoms are, not whether the
   diagnosis is right, so a gate of 0.75 removes few wrong actions. Don't present it as a
   safety mechanism without that caveat.
4. **Service ownership** — three people, five components. Who owns what?
5. **Licence** — none chosen yet.
6. **AWS** — the brief rules out EKS because AWS Academy blocks IAM role creation, and
   accounts are individual rather than shared. If any AWS is used, one member owns that
   environment.

## Publishing the repo

Nothing has been pushed. To create the remote:

```bash
git remote add origin https://github.com/<your-username>/autosre.git
git push -u origin main
```
