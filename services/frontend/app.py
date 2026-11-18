import asyncio
import os
import re
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Response
from fastapi.staticfiles import StaticFiles
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
                _json(client, prom, {"query": 'ALERTS{alertstate="firing",namespace="autosre",alertname!~"InfoInhibitor|Watchdog"}'}),     # meta alerts are not incidents
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


try:                                                       # imported as a package in the tests, as a plain module in the container
    from . import history
except ImportError:                                        # pragma: no cover
    import history

STATIC_DIR = Path(__file__).with_name("static")
PAGE = Path(__file__).with_name("index.html").read_text(encoding="utf-8-sig")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/api/history")
async def api_history(limit: int = 500):
    """Everything the on-call pages show: incidents with their stage timelines, setting changes and scenario runs. Read from the engine and the
    dispatcher, which keep their history on a persistent volume that the lab's sync script copies to the S3 bucket."""
    limit = max(1, min(limit, 500))
    async with httpx.AsyncClient(timeout=4.0) as client:
        (inc, e1), (ex, e2), (ev, e3), (dg, _e4) = await asyncio.gather(
            _json(client, f"{ENGINE_URL}/incidents", {"limit": limit}),
            _json(client, f"{DISPATCHER_URL}/executions", {"limit": 100}),
            _json(client, f"{ENGINE_URL}/events", {"limit": 500}),
            _json(client, f"{DISPATCHER_URL}/diagnostics", {"limit": 100}))
    out = history.build((inc or {}).get("incidents", []), (ex or {}).get("executions", []), (ev or {}).get("events", []), (dg or {}).get("diagnostics", []))
    out["errors"] = {k: v for k, v in {"engine": e1 or e3, "dispatcher": e2}.items() if v}
    return out


async def _record(kind: str, body: dict) -> dict:
    who = re.sub(r"[^A-Za-z0-9._ -]", "", str(body.get("by") or "")).strip()[:32]
    incident = str(body.get("id") or "")
    if not who or not re.fullmatch(r"INC-\d{1,8}", incident):
        raise HTTPException(status_code=422, detail="need a signed-in name and an incident id like INC-0007")
    event = {"kind": kind, "name": incident, "by": who}
    if kind == "resolve":
        event["text"] = str(body.get("note") or "")[:200]
    async with httpx.AsyncClient(timeout=4.0) as client:
        try:
            r = await client.post(f"{ENGINE_URL}/events", json=event)
            r.raise_for_status()
            return r.json()
        except Exception as exc:                            # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"could not record it: {type(exc).__name__}") from exc


@app.post("/api/ack")
async def api_ack(body: dict):
    return await _record("ack", body)


@app.post("/api/resolve")
async def api_resolve(body: dict):
    return await _record("resolve", body)


@app.get("/", response_class=HTMLResponse)
async def serve_dashboard():
    return PAGE
