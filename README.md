# AutoSRE: Autonomous Root Cause Analysis & Self-Healing

An automated observability and remediation framework designed for high-availability retail banking infrastructure. AutoSRE continuously observes microservice telemetry, correlates multi-service alert storms via topological graph analysis, and executes automated runbooks to restore operational baselines.

---

## Architecture Topology

The banking transaction pipeline operates across a 4-tier microservice dependency chain:

$$\text{Frontend Ingress} \longrightarrow \text{API Gateway} \longrightarrow \text{Transaction Service} \longrightarrow \text{Database Core}$$

* **Frontend Ingress (`:8000`)**: Executive telemetry dashboard and synthetic transaction monitor.
* **API Gateway (`:8000`)**: Request routing, gateway-level rate accounting, and latency telemetry.
* **Transaction Service (`:8000`)**: Core settlement service with connection pool instrumentation and chaos hooks.
* **Database Core (`:8000`)**: Ledger datastore reporting disk capacity and execution metrics.

---

## Role Ownership & Responsibilities

| Role | Engineer | Domain / Directory | Core Responsibilities |
|---|---|---|---|
| **Web Developer** | Vaishnavi | `services/`, `k8s/loadgen.yaml` | Service tier runtime, chaos hooks, traffic generator, executive UI |
| **Cloud Engineer** | Shivani | `k8s/`, `monitoring/`, `ansible/` | Cluster orchestration, Prometheus/Alertmanager, Ansible dispatcher |
| **ML Engineer** | Skandesh | `correlation-engine/`, `tests/` | Signature matching, graph-depth traversal, confidence model |

---

## Cross-Role Interface Contracts

1. **Contract #1 (Webdev $\rightarrow$ ML):** Every application metric emitted via `/metrics` must contain a `service` label.
2. **Contract #2 (Cloud $\leftrightarrow$ ML):** Alert names in `monitoring/alert-rules.yaml` must map 1:1 to symptom names in `correlation-engine/signatures.py`.
3. **Contract #3 (ML $\rightarrow$ Cloud):** Correlation engine exports `{root_cause, service, confidence, playbook}` to the remediation dispatcher.
4. **Contract #4 (ML $\leftrightarrow$ Cloud):** Root-cause keys in `ansible/remediation_map.yml` must match keys in `signatures.py`.

---

## Completed Deliverables (Milestone 1 — Web Developer)

- [x] **Continuous Load Generator (`k8s/loadgen.yaml`)**: Deploys continuous ~5 req/s synthetic ingress traffic to enable non-zero Prometheus rate calculations.
- [x] **Chaos Fault Injection (`services/transaction-service`)**: Dynamic fault endpoints (`/chaos/pool`, `/chaos/errors`, `/chaos/latency`, `/chaos/memory`, `/chaos/reset`) protected by `CHAOS_ENABLED` flag.
- [x] **Database Capacity Chaos (`services/database`)**: `/chaos/disk` hook to trip `DBDiskNearFull` scenarios on demand.
- [x] **Executive Operations Dashboard (`services/frontend`)**: Two-tier UI featuring high-level executive health KPIs and real-time transaction telemetry logs.
- [x] **Non-Root Container Security (`services/*/Dockerfile`)**: Hardened Docker specifications enforcing non-privileged system user runtime (`UID 10001`).
- [x] **Automated Test Suite (`tests/test_chaos.py`)**: Unit and integration test coverage validating safety guardrails and metric collection.

---

## Local Development Quickstart

```powershell
# 1. Activate virtual environment
.\.venv\Scripts\Activate.ps1

# 2. Run unit tests
python -m pytest tests/test_chaos.py -v

# 3. Launch Frontend UI locally
uvicorn services.frontend.app:app --port 3000
