import os
import httpx
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

app = FastAPI(title="frontend")

GATEWAY_URL = os.getenv("GATEWAY_URL", "http://api-gateway.autosre.svc.cluster.local:8000")

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
      <title>AutoSRE Banking Core</title>
      <style>
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #0f172a; color: #f8fafc; padding: 2rem; }
        .card { background: #1e293b; border-radius: 8px; padding: 1.5rem; max-width: 650px; margin: auto; box-shadow: 0 4px 6px rgba(0,0,0,0.3); }
        .badge { padding: 4px 10px; border-radius: 4px; font-weight: bold; display: inline-block; }
        .ok { background: #16a34a; }
        .fail { background: #dc2626; }
        table { width: 100%; border-collapse: collapse; margin-top: 1rem; }
        td, th { padding: 8px; text-align: left; border-bottom: 1px solid #334155; }
      </style>
    </head>
    <body>
      <div class="card">
        <h2>AutoSRE — Retail Core Status</h2>
        <div id="status" class="badge ok">Operational</div>
        <table>
          <thead><tr><th>Time</th><th>HTTP Status</th><th>Latency</th></tr></thead>
          <tbody id="rows"></tbody>
        </table>
      </div>
      <script>
        async function runCheck() {
          const start = performance.now();
          const res = await fetch('/api/poll');
          const data = await res.json();
          const latency = Math.round(performance.now() - start);
          const ok = data.status === 200;
          
          const badge = document.getElementById('status');
          badge.className = 'badge ' + (ok ? 'ok' : 'fail');
          badge.innerText = ok ? 'Operational' : 'Degraded (5xx Outage)';

          const row = `<tr><td>${new Date().toLocaleTimeString()}</td><td>${data.status}</td><td>${latency}ms</td></tr>`;
          const rows = document.getElementById('rows');
          rows.insertAdjacentHTML('afterbegin', row);
          if (rows.children.length > 7) rows.removeChild(rows.lastChild);
        }
        setInterval(runCheck, 1500);
      </script>
    </body>
    </html>
    """
