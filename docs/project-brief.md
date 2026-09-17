# AutoSRE — AI-Assisted Root Cause Analysis & Self-Healing for Banking Infrastructure

**Course**: DevOps team project (Banking & Finance domain)
**Team**: Skandesh, Shivani, Vaishnavi
**Repo name**: TBD — suggest `autosre-banking` unless the team has already picked one

---

## 1. Objective

Build a self-healing observability system that automatically correlates symptoms across
dependent banking services, identifies the true root cause of an incident, and triggers
recovery — without a human manually triaging alerts.

## 2. Problem Statement

When a banking service fails, multiple symptom alerts fire simultaneously (CPU high, API
latency high, DB connection failed, pod restarting, error rate high). The real cause is
usually just one of these. Diagnosing it manually costs minutes during a live incident —
minutes a bank cannot afford, since downtime on customer-facing services means failed
transactions.

## 3. Architecture (exact — build to this spec)

### 3a. Service dependency chain

```
Frontend
   │
   ▼
API Gateway
   │
   ▼
Transaction Service
   │
   ▼
Database
```

Each box is a separately containerized service, deployed as its own Kubernetes
Deployment + Service, wired together via K8s internal DNS (e.g. API Gateway's config
points at `transaction-service.<namespace>.svc.cluster.local`).

### 3b. Correlation & recovery flow

```
Prometheus Metrics + Kubernetes Events + App Logs
                 │
                 ▼
        Correlation Engine
   (time-window grouping → dependency-
    graph root-cause ranking → symptom-
    signature matching → confidence score)
                 │
                 ▼
         Ranked Root Cause
        (e.g. "db_connection_pool_exhaustion", 91%)
                 │
                 ▼
       Automated Remediation
   (root-cause label → Ansible playbook
    lookup table → ansible-playbook run)
```

### 3c. End-to-end example

```
Symptoms fire together:
  API Latency ↑        (api-gateway)
  Error Rate ↑          (transaction-service)
  Pod Restarts ↑        (transaction-service)

Correlation engine walks the dependency chain from the DB end upward,
finds the deepest alerting service, checks which specific metric crossed
threshold there → outputs:

  ROOT CAUSE: Database connection pool exhaustion
  CONFIDENCE: 91%
  ACTION: Restart affected service

Dispatcher looks up "db_connection_pool_exhaustion" in the remediation
table → runs restart_service.yml against transaction-service.
```

## 4. Tech Stack — exact tools and their role

| Stage | Tool | Role |
|---|---|---|
| Build | **Docker** | Containerize each service (Frontend, API Gateway, Transaction Service, Database/mock DB). Each app service must expose a `/metrics` endpoint (use `prom-client` for Node.js or `prometheus_client` for Python). |
| Deploy | **Kubernetes** | Deployment + Service per component, with resource limits set low enough to trigger real symptoms on demand, and liveness/readiness probes configured (without these, pod-restart symptoms won't fire naturally). |
| Monitor | **Prometheus** | Scrapes custom app metrics (transaction count, error count, latency, `db_connection_pool_active`) plus infra metrics via `kube-state-metrics` (pod restarts) and `node-exporter` (CPU/memory). Alertmanager rules fire the correlation logic. |
| Monitor | **Grafana** | Visualization only — dashboards for latency/error-rate time series, pod restart count, and the correlation engine's confidence score if pushed as a metric. |
| Recover | **Ansible** | One playbook per known root cause (`restart_service.yml`, `scale_replicas.yml`, etc.), using the `kubernetes.core` collection. Triggered either by a custom webhook script or by **Event-Driven Ansible** (`ansible-rulebook` + `ansible.eda` collection — free, open source; the polished paid "EDA Controller" UI is not needed for this project). |

**Deployment platform constraint**: Use **minikube or k3s**, not AWS EKS. The team's AWS
Academy Learner Lab access blocks new IAM role creation (confirmed via an
`iam:CreateGroup` denial during earlier coursework), and EKS requires creating both a
cluster service role and a node-group role. Self-managed K8s on a single node sidesteps
this entirely. AWS Academy accounts are also individual, not shared across teammates —
if any AWS is used at all, plan for one team member to own that environment, with the
rest of the team contributing through the shared repo.

## 5. Correlation Algorithm (implement as described — rule-based, not ML, unless time allows)

1. **Time-window grouping**: bucket alerts arriving within ~30–60s into one incident via
   the Alertmanager webhook receiver.
2. **Dependency graph lookup**: map each alert's `service` label to its position in the
   chain (`database` → `transaction-service` → `api-gateway` → `frontend`).
3. **Root cause ranking**: problems cascade upward, not downward — the root cause is the
   deepest alerting service in the chain (closest to the database).
4. **Symptom-signature matching**: for the identified service, match which specific
   metric crossed threshold against a predefined signature table (e.g.
   `db_connection_pool_active > 0.9 * pool_max` → `db_connection_pool_exhaustion`).
5. **Confidence score**: `matched_symptoms / expected_symptoms_for_this_cause` —
   simple, explainable ratio, not a black-box probability. (Optional stretch goal: replace
   this with a small `scikit-learn` classifier trained on synthetic labeled data.)

## 6. Root Cause → Remediation Mapping (extend as needed; start with 2–3 for the demo)

| Root cause label | Playbook | Action |
|---|---|---|
| `db_connection_pool_exhaustion` | `restart_service.yml` | Restart the affected service/pod |
| `high_memory_usage` | `scale_replicas.yml` | Increase replica count via `kubectl scale` |
| `db_disk_full` | `clear_logs.yml` | Clear old log files on the affected node |

Unmapped root causes should fall back to alerting a human, not guessing a fix.

## 7. Suggested Build Order

1. **Base app**: containerize the 4 services with Docker, wire dependency chain, deploy
   to minikube. Confirm inter-service calls work end to end.
2. **Metrics**: add `/metrics` endpoints to each service; install `kube-prometheus-stack`
   via Helm; confirm Prometheus is scraping real data; build initial Grafana dashboards.
3. **Fault injection**: manually trigger each symptom (kill a pod, spike a mock
   `db_connection_pool_active` value, etc.) and confirm Prometheus/Alertmanager fires.
4. **Correlation engine**: implement Steps 1–5 above as a standalone script/service that
   consumes the Alertmanager webhook.
5. **Remediation**: write the Ansible playbooks and the root-cause → playbook dispatch
   (custom script first; swap in Event-Driven Ansible if time allows).
6. **End-to-end demo run**: trigger the exact scenario in Section 3c live, confirm Grafana
   shows the dip-and-recovery, confirm the dispatcher runs the correct playbook.

## 8. Deliverables Checklist (per course requirements)

- [ ] Git repo with working code (this stage: at minimum, project name/repo created)
- [ ] Project title, objective, problem statement (this doc covers all three)
- [ ] At least one DevOps tool used — this design uses five (Docker, Kubernetes,
      Prometheus, Grafana, Ansible), covering the full Build → Deploy → Monitor →
      Recover loop as required
- [ ] Innovation framing: event-driven correlation + remediation, applying the same
      pattern used by Red Hat's Event-Driven Ansible and enterprise AIOps tools
      (BigPanda, Moogsoft, BMC Helix), purpose-built for a banking dependency chain
- [ ] Society angle: faster, correct incident recovery reduces failed transactions and
      downtime on customer-facing banking systems
