import os
from typing import Any, Literal

import httpx
from fastapi import FastAPI, Response
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
from fastapi.responses import HTMLResponse
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StrictBool, ValidationError

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
REMEDIATION_URL = os.getenv("REMEDIATION_URL", "http://remediation-dispatcher.autosre.svc.cluster.local:8001")


class RecordedJob(BaseModel):
    id: str
    root_cause: str
    service: str
    requested_at: float = Field(ge=0, allow_inf_nan=False, strict=True)
    status: Literal["accepted", "running", "succeeded", "failed", "not_needed"]
    confidence: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False, strict=True)
    playbook: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class DispatcherSettings(BaseModel):
    auto_remediate: StrictBool
    min_confidence: float = Field(ge=0, le=1, allow_inf_nan=False, strict=True)
    cooldown_seconds: int = Field(ge=0, strict=True)


class RecordedCooldown(BaseModel):
    active: StrictBool
    remaining_seconds: int = Field(ge=0, strict=True)
    operator_review_required: StrictBool


class RemediationTelemetry(BaseModel):
    status: Literal["ok", "empty"]
    remediation: RecordedJob | None
    settings: DispatcherSettings
    cooldown: RecordedCooldown


@app.get("/api/remediation")
async def remediation_endpoint():
    """Same-origin, read-only history proxy; never dispatches or probes transactions."""
    headers = {"Cache-Control": "no-store"}
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            response = await client.get(f"{REMEDIATION_URL.rstrip('/')}/remediations/latest")
            response.raise_for_status()
            telemetry = RemediationTelemetry.model_validate(response.json())
        if (telemetry.status == "empty") != (telemetry.remediation is None):
            raise ValueError("Inconsistent remediation snapshot")
        return JSONResponse(telemetry.model_dump(), headers=headers)
    except (httpx.HTTPError, httpx.InvalidURL, ValueError, ValidationError):
        return JSONResponse({"status": "unavailable", "remediation": None, "settings": None, "cooldown": None},
                            status_code=503, headers=headers)

@app.get("/api/poll")
async def poll_endpoint():
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.get(f"{GATEWAY_URL}/api/v1/trade")
            return {"status": resp.status_code}
    except Exception:
        return {"status": 503}

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
              <div id="dash-remediation-status" class="card-val" style="color: var(--status-ok); font-size: 1.45rem;">Loading history</div>
              <p id="dash-remediation-mode" style="font-size: 0.75rem; color: var(--text-muted); margin-top: 0.35rem;">Checking remediation settings</p>
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
                <p id="dash-incident-desc" style="font-size: 0.85rem; color: var(--text-secondary);">Loading recorded remediation history; transaction health is shown separately.</p>
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
              <div id="stat-pool" class="card-val">2 / 20</div>
              <div style="font-size: 0.72rem; color: var(--text-muted); margin-top: 0.35rem;">Alert rule: &ge; 18 active</div>
            </div>
            <div class="card">
              <div class="card-title">Disk Storage Ratio</div>
              <div id="stat-disk" class="card-val">30%</div>
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

            <!-- Right: Teammates Modules Placeholders -->
            <div style="display: flex; flex-direction: column; gap: 1.25rem;">
              
              <!-- ML Engineer Placeholder -->
              <div class="card" style="border-top: 3px solid var(--gold-accent);">
                <div class="card-header-bar">
                  <span class="card-title">AI Root Cause Correlation Engine</span>
                  <span class="placeholder-tag">ML Engineer</span>
                </div>
                <p style="font-size: 0.8rem; color: var(--text-secondary); margin-top: 0.25rem;">
                  Alertmanager webhook consumer utilizing topological depth traversal and signature weighting.
                </p>
                <div class="placeholder-box">
                  <div style="font-size: 0.72rem; font-weight: 600; color: var(--navy-mid);">Latest recorded diagnosis:</div>
                  <pre id="correlation-payload" class="code-snippet" style="white-space: pre-wrap; overflow-wrap: anywhere;">Loading recorded diagnosis</pre>
                  <p id="confidence-note" style="font-size: 0.75rem; margin-top: 0.5rem;">Rule confidence: checking recorded value</p>
                  <p id="job-requested-at" style="font-size: 0.72rem; color: var(--text-muted); margin-top: 0.5rem;">Recorded request time pending</p>
                </div>
              </div>

              <!-- Cloud Engineer Placeholder -->
              <div class="card" style="border-top: 3px solid var(--navy-mid);">
                <div class="card-header-bar">
                  <span class="card-title">Ansible Remediation Dispatcher</span>
                  <span class="placeholder-tag" style="background: rgba(23, 50, 82, 0.1); color: var(--navy-mid); border-color: rgba(23, 50, 82, 0.25);">Cloud Engineer</span>
                </div>
                <p style="font-size: 0.8rem; color: var(--text-secondary); margin-top: 0.25rem;">
                  In-cluster RBAC runner executing remediation playbooks with lockouts.
                </p>
                <div class="placeholder-box">
                  <div style="display: flex; justify-content: space-between; align-items: center;">
                    <span style="font-size: 0.75rem; color: var(--text-secondary); font-weight: 600;">Cooldown Protection:</span>
                    <span id="remediation-cooldown" style="font-size: 0.75rem; font-family: 'JetBrains Mono', monospace; color: var(--status-ok); font-weight: 700;">Checking</span>
                  </div>
                  <div style="display: flex; justify-content: space-between; align-items: center; margin-top: 0.5rem;">
                    <span style="font-size: 0.75rem; color: var(--text-secondary); font-weight: 600;">Confidence Threshold:</span>
                    <span id="remediation-threshold" style="font-size: 0.75rem; font-family: 'JetBrains Mono', monospace; color: var(--navy-mid); font-weight: 700;">Checking</span>
                  </div>
                  <div style="font-size: 0.75rem; margin-top: 0.75rem;">
                    Recorded job: <span id="remediation-job-status">Loading history</span>
                    <div id="remediation-job-id" style="font-family: 'JetBrains Mono', monospace; overflow-wrap: anywhere; margin-top: 0.35rem;"></div>
                  </div>
                  <p style="font-size: 0.72rem; color: var(--text-muted); margin-top: 0.75rem;">Recorded recovery evidence (historical, not current service health):</p>
                  <pre id="recovery-evidence" class="code-snippet" style="white-space: pre-wrap; overflow-wrap: anywhere;">Loading history</pre>
                </div>
              </div>

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

        function renderRemediation(data) {
          const available = data.status === 'ok' || data.status === 'empty';
          const job = available ? data.remediation : null;
          const text = (id, value) => { document.getElementById(id).textContent = value; };
          const missing = available ? 'No remediation recorded' : 'Telemetry unavailable';
          text('dash-remediation-status', job ? 'Last: ' + job.status : missing);
          text('remediation-job-status', job ? job.status : missing);
          text('remediation-job-id', job ? job.id : '');
          text('correlation-payload', job ? JSON.stringify({
            root_cause: job.root_cause, service: job.service,
            confidence: job.confidence, playbook: job.playbook
          }, null, 2) : missing);
          text('confidence-note', 'Rule confidence: ' + (job && job.confidence != null ? String(job.confidence) : 'Not recorded'));
          const requested = job ? new Date(job.requested_at * 1000) : null;
          text('job-requested-at', requested && !Number.isNaN(requested.getTime())
            ? 'Requested: ' + requested.toLocaleString() : 'No recorded request time');
          const details = job ? job.details : {};
          const proof = {};
          for (const key of ['previous_generation', 'generation', 'previous_instance', 'recovery', 'reason']) {
            if (Object.prototype.hasOwnProperty.call(details, key)) proof[key] = details[key];
          }
          text('recovery-evidence', Object.keys(proof).length ? JSON.stringify(proof, null, 2)
            : (available ? 'No recovery evidence recorded' : 'Telemetry unavailable'));
          text('dash-incident-desc', job
            ? 'Latest recorded remediation: ' + job.status + ' for ' + job.service + '. Current transaction health is shown separately.'
            : missing + '. Current transaction health is shown separately.');
          if (available) {
            text('dash-remediation-mode', data.settings.auto_remediate ? 'Automatic remediation enabled' : 'Dry-run mode');
            text('remediation-threshold', '\u2265 ' + data.settings.min_confidence);
            text('remediation-cooldown', data.cooldown.operator_review_required ? 'Operator review required'
              : data.cooldown.active ? 'Active (' + data.cooldown.remaining_seconds + 's remaining)'
              : 'Configured (' + data.settings.cooldown_seconds + 's); inactive');
          } else {
            for (const id of ['dash-remediation-mode', 'remediation-threshold', 'remediation-cooldown']) text(id, 'Unavailable');
          }
        }

        async function pollRemediation() {
          const controller = new AbortController();
          const timer = setTimeout(() => controller.abort(), 4000);
          try {
            const response = await fetch('/api/remediation', {cache: 'no-store', signal: controller.signal});
            if (!response.ok) throw new Error('Telemetry unavailable');
            renderRemediation(await response.json());
          } catch (error) {
            renderRemediation({status: 'unavailable'});
          } finally {
            clearTimeout(timer);
            // Schedule after completion so slow backends cannot overlap polls.
            setTimeout(pollRemediation, 5000);
          }
        }

        setInterval(pollStatus, 1500);
        pollRemediation();
      </script>
    </body>
    </html>
    """
