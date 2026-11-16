import asyncio
import os
import httpx
from fastapi import FastAPI, Response
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
from fastapi.responses import HTMLResponse

app = FastAPI(title="frontend")
@app.get("/metrics")
async def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

@app.get("/health")
async def health():
    return {"status": "ok", "service": "frontend"}

@app.get("/ready")
async def ready():
    return {"status": "ready", "service": "frontend"}

GATEWAY_URL = os.getenv("GATEWAY_URL", "http://api-gateway.autosre.svc.cluster.local:8000")

@app.get("/api/poll")
async def poll_endpoint():
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.get(f"{GATEWAY_URL}/api/v1/trade")
            return {"status": resp.status_code}
    except Exception:
        return {"status": 503}

ENGINE_URL = os.getenv("ENGINE_URL", "http://correlation-engine.autosre.svc.cluster.local:8000")
DISPATCHER_URL = os.getenv("DISPATCHER_URL", "http://remediation-dispatcher.autosre.svc.cluster.local:8001")
PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://monitoring-kube-prometheus-prometheus.monitoring.svc.cluster.local:9090")


async def _json(client, url, params=None):
    """(data, None) or (None, error). One slow or dead dependency must never break the page."""
    try:
        r = await client.get(url, params=params)
        r.raise_for_status()
        return r.json(), None
    except Exception as exc:                                    # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"


def _prom_value(data):
    try:
        return float(data["data"]["result"][0]["value"][1])
    except (TypeError, KeyError, IndexError, ValueError):
        return None


@app.get("/api/state")
async def state():
    """Everything the dashboard shows beyond the probe: alerts, the engine's latest decisions, the dispatcher's executions and
    rollouts, and the live pool and disk numbers. Each part is independent and carries its own error."""
    prom = f"{PROMETHEUS_URL}/api/v1/query"
    async with httpx.AsyncClient(timeout=2.5) as client:
        (incidents, e1), (executions, e2), (rollouts, e3), (health, e4), (cool, e5), (alerts, e6), (pool, e7), (pmax, e8), (disk, e9) = \
            await asyncio.gather(
                _json(client, f"{ENGINE_URL}/incidents", {"limit": 10}),
                _json(client, f"{DISPATCHER_URL}/executions", {"limit": 10}),
                _json(client, f"{DISPATCHER_URL}/deployments"),
                _json(client, f"{DISPATCHER_URL}/health"),
                _json(client, f"{DISPATCHER_URL}/state"),
                _json(client, prom, {"query": 'ALERTS{alertstate="firing",namespace="autosre"}'}),
                _json(client, prom, {"query": 'max(db_connection_pool_active{service="transaction-service"})'}),
                _json(client, prom, {"query": 'max(db_connection_pool_max{service="transaction-service"})'}),
                _json(client, prom, {"query": 'max(db_disk_usage_ratio{service="database"})'}))
    firing = []
    try:
        firing = [{"alertname": r["metric"].get("alertname"), "service": r["metric"].get("service"),
                   "severity": r["metric"].get("severity")} for r in alerts["data"]["result"]]
    except (TypeError, KeyError):
        pass
    return {
        "incidents": (incidents or {}).get("incidents", []),
        "executions": (executions or {}).get("executions", []),
        "deployments": (rollouts or {}).get("deployments", []),
        "dispatcher": {"health": health, "cooldowns": (cool or {}).get("cooldowns", []), "in_flight": (cool or {}).get("in_flight", [])},
        "alerts": firing,
        "pool": {"active": _prom_value(pool), "max": _prom_value(pmax)},
        "disk": _prom_value(disk),
        "errors": {k: v for k, v in {"engine": e1, "dispatcher": e2 or e3 or e4 or e5, "prometheus": e6 or e7 or e8 or e9}.items() if v},
    }


@app.get("/", response_class=HTMLResponse)
async def serve_dashboard():
    return """
    <!DOCTYPE html>
    <html lang="en">
    <head>
      <meta charset="UTF-8">
      <meta name="viewport" content="width=device-width, initial-scale=1.0">
      <title>AutoSRE | Enterprise Banking Core Operations</title>
      <link href="https://fonts.googleapis.com/css2?family=Cinzel:wght@600;700&family=Plus+Jakarta+Sans:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
      <style>
        :root {
          /* Executive Banking Palette - Warm Parchment & Classic Navy/Gold */
          --navy-deep: #09172A;
          --navy-mid: #173252;
          --navy-subtle: #25446B;
          --gold-accent: #B88E3E;
          --gold-light: #DFC285;
          --gold-soft-bg: rgba(184, 142, 62, 0.12);
          
          /* Warm linen/biscuit parchment background instead of white */
          --parchment-bg: #EAE5DC;
          --card-bg: #F7F5F0;
          --card-subtle-bg: #EEE9DF;
          --border-color: #D6CEBF;
          --border-strong: #C4BAA6;
          
          --text-primary: #0F172A;
          --text-secondary: #3E4B5B;
          --text-muted: #64748B;
          
          --status-ok: #0D825B;
          --status-ok-bg: #E4F5ED;
          --status-err: #C22828;
          --status-err-bg: #FCE8E8;
        }

        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
          font-family: 'Plus Jakarta Sans', sans-serif;
          background-color: var(--parchment-bg);
          color: var(--text-primary);
          min-height: 100vh;
          display: flex;
          flex-direction: column;
        }

        /* Top Institutional Header */
        header {
          display: flex;
          align-items: center;
          justify-content: space-between;
          padding: 1.15rem 3rem;
          background: var(--navy-deep);
          color: #ffffff;
          border-bottom: 2px solid var(--gold-accent);
          box-shadow: 0 4px 14px rgba(9, 23, 42, 0.15);
          position: sticky;
          top: 0;
          z-index: 100;
        }
        .logo-wrap {
          display: flex;
          align-items: center;
          gap: 1rem;
        }
        .logo-symbol {
          background: linear-gradient(135deg, var(--gold-accent), var(--gold-light));
          color: var(--navy-deep);
          font-family: 'Cinzel', serif;
          font-weight: 700;
          padding: 0.35rem 0.65rem;
          border-radius: 4px;
          font-size: 0.95rem;
          letter-spacing: 1px;
        }
        .brand-meta {
          display: flex;
          flex-direction: column;
        }
        .brand-title {
          font-family: 'Cinzel', serif;
          font-size: 1.1rem;
          font-weight: 700;
          letter-spacing: 0.5px;
          color: #ffffff;
        }
        .brand-tag {
          font-size: 0.72rem;
          color: var(--gold-light);
          text-transform: uppercase;
          letter-spacing: 1.2px;
          font-weight: 600;
        }

        .nav-links {
          display: flex;
          gap: 0.5rem;
          background: rgba(255, 255, 255, 0.08);
          padding: 0.25rem;
          border-radius: 6px;
          border: 1px solid rgba(255, 255, 255, 0.14);
        }
        .nav-btn {
          background: transparent;
          border: none;
          color: #94A3B8;
          padding: 0.55rem 1.4rem;
          border-radius: 4px;
          cursor: pointer;
          font-weight: 600;
          font-size: 0.85rem;
          transition: all 0.2s ease;
        }
        .nav-btn.active {
          background: var(--gold-accent);
          color: var(--navy-deep);
          box-shadow: 0 2px 6px rgba(0,0,0,0.25);
        }

        /* Container Layout */
        .container {
          flex: 1;
          max-width: 1220px;
          margin: 0 auto;
          width: 100%;
          padding: 2.5rem 1.5rem;
        }
        .view-section { display: none; }
        .view-section.active { display: block; animation: fadeIn 0.3s ease-out forwards; }

        @keyframes fadeIn {
          from { opacity: 0; transform: translateY(6px); }
          to { opacity: 1; transform: translateY(0); }
        }

        /* Grids & Cards */
        .grid-4 { display: grid; grid-template-columns: repeat(4, 1fr); gap: 1.25rem; margin-bottom: 2rem; }
        .grid-2 { display: grid; grid-template-columns: 1.8fr 1.2fr; gap: 1.75rem; }

        .card {
          background: var(--card-bg);
          border: 1px solid var(--border-color);
          border-radius: 8px;
          padding: 1.5rem;
          box-shadow: 0 2px 5px rgba(9, 23, 42, 0.04);
          position: relative;
        }
        .card-clickable {
          cursor: pointer;
          transition: transform 0.18s ease, box-shadow 0.18s ease, border-color 0.18s ease;
        }
        .card-clickable:hover {
          transform: translateY(-2px);
          border-color: var(--gold-accent);
          box-shadow: 0 6px 16px rgba(9, 23, 42, 0.08);
        }
        .card-header-bar {
          display: flex;
          justify-content: space-between;
          align-items: center;
          margin-bottom: 0.6rem;
        }
        .card-title {
          font-size: 0.75rem;
          text-transform: uppercase;
          letter-spacing: 0.08em;
          color: var(--text-muted);
          font-weight: 700;
        }
        .card-val {
          font-size: 1.85rem;
          font-weight: 700;
          color: var(--navy-deep);
          font-family: 'JetBrains Mono', monospace;
        }

        /* Status Badges */
        .badge {
          display: inline-flex;
          align-items: center;
          gap: 0.45rem;
          padding: 0.35rem 0.85rem;
          border-radius: 4px;
          font-size: 0.75rem;
          font-weight: 700;
          letter-spacing: 0.5px;
          text-transform: uppercase;
        }
        .badge.ok {
          background: var(--status-ok-bg);
          color: var(--status-ok);
          border: 1px solid rgba(13, 130, 91, 0.3);
        }
        .badge.fail {
          background: var(--status-err-bg);
          color: var(--status-err);
          border: 1px solid rgba(194, 40, 40, 0.3);
        }
        .badge.pulse::before {
          content: "";
          width: 7px;
          height: 7px;
          border-radius: 50%;
          background: currentColor;
          box-shadow: 0 0 6px currentColor;
        }

        /* Service Dependency Topology */
        .pipeline {
          display: flex;
          align-items: center;
          justify-content: space-between;
          padding: 1.75rem 1.25rem;
          margin-top: 1rem;
          background: var(--card-subtle-bg);
          border-radius: 6px;
          border: 1px solid var(--border-color);
        }
        .node {
          text-align: center;
          padding: 1.1rem 1.4rem;
          background: var(--card-bg);
          border: 1px solid var(--border-color);
          border-radius: 6px;
          min-width: 175px;
          box-shadow: 0 1px 3px rgba(0,0,0,0.03);
          border-top: 3px solid var(--navy-mid);
        }
        .node-name { font-size: 0.85rem; font-weight: 700; color: var(--navy-deep); margin-bottom: 0.25rem; }
        .node-stat { font-size: 0.75rem; font-family: 'JetBrains Mono', monospace; color: var(--text-secondary); }
        .arrow { color: var(--gold-accent); font-size: 1.3rem; font-weight: bold; }

        /* Tables & Telemetry logs */
        table {
          width: 100%;
          border-collapse: collapse;
          margin-top: 0.75rem;
          font-family: 'JetBrains Mono', monospace;
          font-size: 0.82rem;
        }
        th {
          text-align: left;
          padding: 0.75rem 1rem;
          color: var(--text-muted);
          background: var(--card-subtle-bg);
          border-bottom: 2px solid var(--border-color);
          font-weight: 600;
          font-size: 0.72rem;
          text-transform: uppercase;
          letter-spacing: 0.05em;
        }
        td {
          padding: 0.8rem 1rem;
          border-bottom: 1px solid var(--border-color);
          color: var(--text-secondary);
        }
        tr:hover td {
          background-color: var(--card-subtle-bg);
        }

        /* Module Placeholders */
        .placeholder-box {
          border: 1px dashed var(--border-strong);
          background: var(--card-subtle-bg);
          border-radius: 6px;
          padding: 1.25rem;
          margin-top: 1rem;
        }
        .placeholder-tag {
          font-size: 0.68rem;
          background: var(--gold-soft-bg);
          color: #7A5714;
          border: 1px solid rgba(184, 142, 62, 0.4);
          padding: 0.2rem 0.55rem;
          border-radius: 3px;
          font-weight: 700;
          text-transform: uppercase;
          letter-spacing: 0.5px;
        }
        table.kv { margin-top: 0.25rem; }
        table.kv td:first-child { width: 42%; color: var(--text-muted); font-weight: 600; }
        .code-snippet {
          font-family: 'JetBrains Mono', monospace;
          font-size: 0.78rem;
          color: var(--navy-deep);
          background: var(--card-bg);
          padding: 0.85rem;
          border-radius: 4px;
          border: 1px solid var(--border-color);
          margin-top: 0.6rem;
          line-height: 1.45;
        }
      </style>
    </head>
    <body>

      <header>
        <div class="logo-wrap">
          <div class="logo-symbol">SR</div>
          <div class="brand-meta">
            <span class="brand-title">AUTOSRE RETAIL CORE</span>
            <span class="brand-tag">Autonomous Reliability & Telemetry</span>
          </div>
        </div>
        <div class="nav-links">
          <button id="nav-dash" class="nav-btn active" onclick="switchView('dashboard')">Executive Overview</button>
          <button id="nav-status" class="nav-btn" onclick="switchView('status')">Telemetry & Deep-Dive</button>
        </div>
      </header>

      <main class="container">
        <!-- ==================== VIEW 1: EXECUTIVE DASHBOARD ==================== -->
        <section id="view-dashboard" class="view-section active">
          <div class="grid-4">
            <div class="card card-clickable" onclick="switchView('status')" style="border-left: 4px solid var(--gold-accent);">
              <div class="card-header-bar">
                <span class="card-title">Banking System Status</span>
              </div>
              <div id="dash-badge" class="badge ok pulse">Operational</div>
              <p style="font-size: 0.75rem; color: var(--text-muted); margin-top: 0.85rem;">Click to inspect live telemetry &rarr;</p>
            </div>
            
            <div class="card" style="border-left: 4px solid var(--navy-mid);">
              <div class="card-header-bar">
                <span class="card-title">End-to-End Latency</span>
              </div>
              <div id="dash-latency" class="card-val">-- ms</div>
              <p style="font-size: 0.75rem; color: var(--text-muted); margin-top: 0.35rem;">Customer transaction round-trip</p>
            </div>

            <div class="card" style="border-left: 4px solid var(--navy-mid);">
              <div class="card-header-bar">
                <span class="card-title">Success Availability</span>
              </div>
              <div id="dash-ratio" class="card-val">-- %</div>
              <p style="font-size: 0.75rem; color: var(--text-muted); margin-top: 0.35rem;">Rolling success ratio</p>
            </div>

            <div class="card" style="border-left: 4px solid var(--status-ok);">
              <div class="card-header-bar">
                <span class="card-title">Incident Resolution</span>
              </div>
              <div class="card-val" style="color: var(--status-ok); font-size: 1.45rem;">Autonomous</div>
              <p style="font-size: 0.75rem; color: var(--text-muted); margin-top: 0.35rem;">SRE self-healing active</p>
            </div>
          </div>

          <!-- Dependency Architecture Topology -->
          <div class="card" style="margin-bottom: 2rem;">
            <div class="card-header-bar">
              <span class="card-title">Transaction Dependency Architecture Topology</span>
              <span class="badge ok" style="font-size: 0.7rem;">Mesh Ready</span>
            </div>
            <div class="pipeline">
              <div class="node">
                <div class="node-name">Frontend Ingress</div>
                <div class="node-stat" style="color: var(--navy-mid)">Port 8000</div>
              </div>
              <div class="arrow">&rarr;</div>
              <div class="node">
                <div class="node-name">API Gateway</div>
                <div class="node-stat">Proxy Routing</div>
              </div>
              <div class="arrow">&rarr;</div>
              <div class="node">
                <div class="node-name">Transaction Service</div>
                <div class="node-stat">Core Settlement</div>
              </div>
              <div class="arrow">&rarr;</div>
              <div class="node">
                <div class="node-name">PostgreSQL Store</div>
                <div class="node-stat">Ledger Database</div>
              </div>
            </div>
          </div>

          <!-- Incident Alert Ribbon -->
          <div class="card card-clickable" onclick="switchView('status')" style="border-top: 3px solid var(--navy-deep); background: var(--card-bg);">
            <div style="display: flex; justify-content: space-between; align-items: center;">
              <div>
                <h3 style="font-size: 0.95rem; font-weight: 700; color: var(--navy-deep); margin-bottom: 0.25rem;">Live Incident Triage Dispatcher</h3>
                <p id="dash-incident-desc" style="font-size: 0.85rem; color: var(--text-secondary);">Prometheus telemetry nominal. Cluster running baseline load.</p>
              </div>
              <button class="nav-btn" style="background: var(--navy-deep); color: #ffffff;">Open Live Telemetry &rarr;</button>
            </div>
          </div>
        </section>

        <!-- ==================== VIEW 2: TELEMETRY & AUDIT ==================== -->
        <section id="view-status" class="view-section">
          <!-- Prometheus Metrics Bar -->
          <div class="grid-4">
            <div class="card">
              <div class="card-title">DB Pool Saturation</div>
              <div id="stat-pool" class="card-val">--</div>
              <div style="font-size: 0.72rem; color: var(--text-muted); margin-top: 0.35rem;">Alert: above 90% of the pool maximum</div>
            </div>
            <div class="card">
              <div class="card-title">Disk Storage Ratio</div>
              <div id="stat-disk" class="card-val">--</div>
              <div style="font-size: 0.72rem; color: var(--text-muted); margin-top: 0.35rem;">Alert rule: &ge; 90% full</div>
            </div>
            <div class="card">
              <div class="card-title">Synthetic Ingress Load</div>
              <div class="card-val" style="color: var(--navy-mid);">~5 req/s</div>
              <div style="font-size: 0.72rem; color: var(--text-muted); margin-top: 0.35rem;">k8s/loadgen.yaml active</div>
            </div>
            <div class="card">
              <div class="card-title">Probe Frequency</div>
              <div id="status-badge" class="badge ok pulse">Healthy (200)</div>
              <div style="font-size: 0.72rem; color: var(--text-muted); margin-top: 0.35rem;">Sampling rate: 1.5s</div>
            </div>
          </div>

          <div class="grid-2">
            <!-- Left: Transaction Probe Audit Logs -->
            <div class="card">
              <div class="card-header-bar">
                <span class="card-title">Live Transaction Ledger Audit Stream</span>
                <span style="font-size: 0.72rem; color: var(--text-muted);">Real-time HTTP Ingress</span>
              </div>
              <table>
                <thead>
                  <tr>
                    <th>Timestamp</th>
                    <th>Status Code</th>
                    <th>Latency</th>
                  </tr>
                </thead>
                <tbody id="rows"></tbody>
              </table>
            </div>

            <!-- Right: live root cause and remediation -->
            <div style="display: flex; flex-direction: column; gap: 1.25rem;">

              <div class="card" style="border-top: 3px solid var(--gold-accent);">
                <div class="card-header-bar">
                  <span class="card-title">Latest Incident and Root Cause</span>
                  <span id="inc-badge" class="badge ok">No incident</span>
                </div>
                <table class="kv">
                  <tr><td>Root cause</td><td id="inc-cause">-</td></tr>
                  <tr><td>Affected service</td><td id="inc-service">-</td></tr>
                  <tr><td>Confidence</td><td id="inc-conf">-</td></tr>
                  <tr><td>Symptoms matched</td><td id="inc-sym">-</td></tr>
                  <tr><td>ML second opinion</td><td id="inc-ml">-</td></tr>
                  <tr><td>Selected playbook</td><td id="inc-play">-</td></tr>
                  <tr><td>Dispatcher decision</td><td id="inc-disp">-</td></tr>
                  <tr><td>Received</td><td id="inc-time">-</td></tr>
                </table>
              </div>

              <div class="card" style="border-top: 3px solid var(--navy-mid);">
                <div class="card-header-bar">
                  <span class="card-title">Ansible Remediation Dispatcher</span>
                  <span id="disp-mode" class="badge ok">dry run</span>
                </div>
                <table class="kv">
                  <tr><td>Confidence threshold</td><td id="disp-thr">-</td></tr>
                  <tr><td>ML policy</td><td id="disp-pol">-</td></tr>
                  <tr><td>Cooldown</td><td id="disp-cool">none active</td></tr>
                </table>
                <table style="margin-top: 0.75rem;">
                  <thead><tr><th>Playbook</th><th>Status</th><th>Time</th></tr></thead>
                  <tbody id="exec-rows"><tr><td colspan="3" style="color: var(--text-muted);">no executions yet</td></tr></tbody>
                </table>
              </div>

            </div>
          </div>

          <div class="grid-2" style="margin-top: 1.75rem;">
            <div class="card">
              <div class="card-header-bar">
                <span class="card-title">Active Alerts (Prometheus)</span>
                <span id="alert-count" class="badge ok">0</span>
              </div>
              <div id="alert-list" style="font-family: 'JetBrains Mono', monospace; font-size: 0.8rem; color: var(--text-secondary);">none firing</div>
            </div>
            <div class="card">
              <div class="card-header-bar">
                <span class="card-title">Rollout and Recovery</span>
                <span id="recovery-badge" class="badge ok">No incident</span>
              </div>
              <table>
                <thead><tr><th>Deployment</th><th>Ready</th><th>Updated</th><th>Rollout</th></tr></thead>
                <tbody id="rollout-rows"><tr><td colspan="4" style="color: var(--text-muted);">waiting for the dispatcher</td></tr></tbody>
              </table>
              <p id="exec-steps" style="font-size: 0.72rem; color: var(--text-muted); margin-top: 0.6rem;"></p>
            </div>
          </div>
        </section>
      </main>

      <script>
        function switchView(viewName) {
          document.getElementById('view-dashboard').classList.remove('active');
          document.getElementById('view-status').classList.remove('active');
          document.getElementById('nav-dash').classList.remove('active');
          document.getElementById('nav-status').classList.remove('active');

          if (viewName === 'dashboard') {
            document.getElementById('view-dashboard').classList.add('active');
            document.getElementById('nav-dash').classList.add('active');
          } else {
            document.getElementById('view-status').classList.add('active');
            document.getElementById('nav-status').classList.add('active');
          }
        }

        let totalAttempts = 0;
        let successfulAttempts = 0;

        async function pollStatus() {
          const start = performance.now();
          const rows = document.getElementById('rows');
          const statusBadge = document.getElementById('status-badge');
          const dashBadge = document.getElementById('dash-badge');
          const dashLatency = document.getElementById('dash-latency');
          const dashRatio = document.getElementById('dash-ratio');
          const incidentDesc = document.getElementById('dash-incident-desc');
          
          totalAttempts++;
          
          try {
            const res = await fetch('/api/poll');
            const data = await res.json();
            const latency = Math.round(performance.now() - start);
            const isOk = data.status === 200;

            if (isOk) successfulAttempts++;

            const badgeClass = isOk ? 'badge ok pulse' : 'badge fail pulse';
            const badgeText = isOk ? 'Operational' : 'Degraded (' + data.status + ')';

            statusBadge.className = badgeClass;
            statusBadge.innerText = badgeText;
            dashBadge.className = badgeClass;
            dashBadge.innerText = badgeText;

            dashLatency.innerText = latency + ' ms';
            dashRatio.innerText = Math.round((successfulAttempts / totalAttempts) * 100) + ' %';

            if (!isOk) {
              incidentDesc.innerText = 'CRITICAL ALERT: HTTP ' + data.status + ' banking service degradation detected. Self-healing triggered.';
              incidentDesc.style.color = 'var(--status-err)';
            } else {
              incidentDesc.innerText = 'Prometheus telemetry nominal. Continuous synthetic load flowing through core.';
              incidentDesc.style.color = 'var(--text-secondary)';
            }

            const row = `<tr><td>${new Date().toLocaleTimeString()}</td><td><strong style="color: ${isOk ? 'var(--status-ok)' : 'var(--status-err)'}">${data.status}</strong></td><td>${latency} ms</td></tr>`;
            rows.insertAdjacentHTML('afterbegin', row);
            if (rows.children.length > 8) rows.removeChild(rows.lastChild);

          } catch (e) {
            statusBadge.className = 'badge fail pulse';
            statusBadge.innerText = 'Network Dropped';
            dashBadge.className = 'badge fail pulse';
            dashBadge.innerText = 'Network Dropped';
            dashLatency.innerText = 'Timeout';
          }
        }

        setInterval(pollStatus, 1500);

        function esc(v) {
          return String(v === null || v === undefined ? '-' : v).replace(/[&<>"']/g, function (c) {
            return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c];
          });
        }
        function setText(id, v) { document.getElementById(id).textContent = (v === null || v === undefined || v === '') ? '-' : v; }
        function badge(id, text, cls) { const e = document.getElementById(id); e.className = 'badge ' + cls; e.textContent = text; }

        function renderState(st) {
          const inc = st.incidents && st.incidents[0];
          const escalated = !!inc && inc.action === 'escalate_to_human';
          if (!inc) {
            badge('inc-badge', 'No incident', 'ok');
          } else {
            badge('inc-badge', escalated ? 'Escalated to a human' : 'Root cause identified', escalated ? 'fail' : 'ok');
            setText('inc-cause', inc.root_cause || '(no known cause)');
            setText('inc-service', inc.service || (inc.alerts && inc.alerts.length ? inc.alerts.map(a => a.service).filter((v, i, a) => a.indexOf(v) === i).join(', ') : '-'));
            setText('inc-conf', inc.confidence === null || inc.confidence === undefined ? '-' : (Math.round(inc.confidence * 100) + ' %'));
            setText('inc-sym', inc.matched_symptoms ? inc.matched_symptoms.join(', ') : (inc.reason || '-'));
            const so = inc.second_opinion;
            setText('inc-ml', so ? so.verdict + (so.scorer_fault ? ' (metrics say ' + so.scorer_fault + ' on ' + so.scorer_service + ')' : '') : 'not consulted');
            setText('inc-play', inc.playbook || '-');
            const rm = inc.remediation;
            setText('inc-disp', rm ? rm.status + (rm.reason ? ': ' + rm.reason : '') : (escalated ? 'not dispatched' : '-'));
            setText('inc-time', inc.received_at);
          }

          const h = st.dispatcher && st.dispatcher.health;
          if (h) {
            badge('disp-mode', h.auto_remediate ? 'live execution ON' : 'dry run', h.auto_remediate ? 'fail' : 'ok');
            setText('disp-thr', h.min_confidence);
            setText('disp-pol', h.second_opinion_policy);
          } else { badge('disp-mode', 'dispatcher unreachable', 'fail'); }
          const cds = (st.dispatcher && st.dispatcher.cooldowns) || [];
          setText('disp-cool', cds.length ? cds.map(c => c.root_cause + ' ' + c.remaining_s + 's').join(', ') : 'none active');

          const rows = document.getElementById('exec-rows');
          const ex = st.executions || [];
          rows.innerHTML = ex.length ? ex.slice(0, 5).map(e => '<tr><td>' + esc(e.playbook) + '</td><td><strong style="color:' +
            (e.status === 'succeeded' ? 'var(--status-ok)' : (e.status === 'running' ? 'var(--navy-mid)' : 'var(--status-err)')) + '">' +
            esc(e.status) + '</strong></td><td>' + esc(e.duration_s !== undefined ? e.duration_s + ' s' : 'running') + '</td></tr>').join('')
            : '<tr><td colspan="3" style="color: var(--text-muted);">no executions yet</td></tr>';
          const last = ex[0];
          document.getElementById('exec-steps').textContent = last ? 'Latest run: ' + last.steps.map(s => s.step).join(' > ') + (last.reason ? ' (' + last.reason + ')' : '') : '';

          const al = st.alerts || [];
          badge('alert-count', String(al.length), al.length ? 'fail' : 'ok');
          document.getElementById('alert-list').innerHTML = al.length ? al.map(a => esc(a.alertname) + ' <span style="color:var(--text-muted)">on ' + esc(a.service) + '</span>').join('<br>') : 'none firing';

          const dep = st.deployments || [];
          document.getElementById('rollout-rows').innerHTML = dep.length ? dep.map(d => d.error ? '<tr><td>' + esc(d.name) + '</td><td colspan="3">' + esc(d.error) + '</td></tr>' :
            '<tr><td>' + esc(d.name) + '</td><td>' + d.ready + ' / ' + d.replicas + '</td><td>' + d.updated + ' / ' + d.replicas + '</td><td>' +
            (d.rolling_out ? '<strong style="color:var(--navy-mid)">rolling out</strong>' : 'complete') + '</td></tr>').join('')
            : '<tr><td colspan="4" style="color: var(--text-muted);">waiting for the dispatcher</td></tr>';

          let rec = ['No incident', 'ok'];
          if (last && last.status === 'running') rec = ['Remediation running', 'fail'];
          else if (last && last.status === 'succeeded' && !al.length) rec = ['Recovered (verified)', 'ok'];
          else if (last && last.status === 'succeeded') rec = ['Fix verified; alerts still clearing', 'fail'];
          else if (last && last.escalate) rec = ['Recovery NOT verified: escalated', 'fail'];
          else if (escalated) rec = ['Escalated: human needed', 'fail'];
          else if (al.length) rec = ['Alerts firing', 'fail'];
          badge('recovery-badge', rec[0], rec[1]);

          if (st.pool && st.pool.active !== null && st.pool.max !== null) setText('stat-pool', Math.round(st.pool.active) + ' / ' + Math.round(st.pool.max));
          if (st.disk !== null && st.disk !== undefined) setText('stat-disk', Math.round(st.disk * 100) + '%');
        }

        async function pollState() {
          try { renderState(await (await fetch('/api/state')).json()); } catch (e) { /* the probe table already shows connectivity */ }
        }
        setInterval(pollState, 3000);
        pollState();

      </script>
    </body>
    </html>
    """
