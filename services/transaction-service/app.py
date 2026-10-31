"""AutoSRE - transaction service.

Position in the banking dependency chain (brief section 3a):

    frontend -> api-gateway -> transaction-service -> database
                               ^^^^^^^^^^^^^^^^^^^

This service owns the database connection pool, so `db_connection_pool_active`
is exported here rather than by the database itself - the pool lives on the
client side. The `db_connection_pool_exhaustion` root cause in brief section 6
is detected from these two gauges.

Endpoints:
    GET /transactions  business route - queries the database
    GET /health        liveness probe
    GET /ready         readiness probe
    GET /metrics       Prometheus metrics
"""

import asyncio
import os
import random
import time

import httpx
from fastapi import FastAPI, HTTPException, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

SERVICE_NAME = os.getenv("SERVICE_NAME", "transaction-service")
DOWNSTREAM_URL = os.getenv("DOWNSTREAM_URL", "http://localhost:8004")
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT_SECONDS", "5"))
DB_POOL_MAX = int(os.getenv("DB_POOL_MAX", "20"))

http_requests_total = Counter(
    "http_requests_total",
    "Total HTTP requests handled.",
    ["service", "path", "status"],
)
http_request_duration_seconds = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency in seconds.",
    ["service", "path"],
)
transactions_total = Counter(
    "transactions_total", "Transactions processed.", ["service"]
)
transaction_errors_total = Counter(
    "transaction_errors_total", "Transactions that failed.", ["service"]
)
db_connection_pool_active = Gauge(
    "db_connection_pool_active",
    "Connections currently checked out of the database pool.",
    ["service"],
)
db_connection_pool_max = Gauge(
    "db_connection_pool_max",
    "Maximum size of the database connection pool.",
    ["service"],
)

db_connection_pool_max.labels(SERVICE_NAME).set(DB_POOL_MAX)
db_connection_pool_active.labels(SERVICE_NAME).set(0)

app = FastAPI(title=f"AutoSRE {SERVICE_NAME}")


@app.get("/metrics")
async def metrics():
    """Serve metrics directly rather than mounting a sub-app.

    A mounted sub-app answers /metrics with a 307 redirect to /metrics/, which
    every scraper then has to follow.
    """
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.middleware("http")
async def record_metrics(request, call_next):
    if request.url.path.startswith("/metrics"):
        return await call_next(request)
    started = time.perf_counter()
    response = await call_next(request)
    http_request_duration_seconds.labels(SERVICE_NAME, request.url.path).observe(
        time.perf_counter() - started
    )
    http_requests_total.labels(
        SERVICE_NAME, request.url.path, str(response.status_code)
    ).inc()
    return response


# --- fault injection ------------------------------------------------------------
# Routes exist only when CHAOS_ENABLED=true, so a normal deployment has no way to
# break itself. Faults are modelled to look like the real thing to the monitoring
# pipeline: a full pool makes requests wait and then fail, rather than merely
# flipping a gauge, so the downstream symptoms (errors, latency) appear too.
CHAOS_ENABLED = os.getenv("CHAOS_ENABLED", "false").lower() == "true"
POOL_WAIT_SECONDS = float(os.getenv("POOL_WAIT_SECONDS", "1.0"))
MEMORY_PRESSURE_DELAY = float(os.getenv("MEMORY_PRESSURE_DELAY_SECONDS", "0.6"))

chaos = {"pool_pinned": 0, "error_fraction": 0.0, "latency_ms": 0, "held": None}
_inflight = 0


def _publish_pool_gauge() -> None:
    db_connection_pool_active.labels(SERVICE_NAME).set(_inflight + chaos["pool_pinned"])


class PoolExhausted(Exception):
    pass


async def call_downstream(path: str) -> dict:
    """Call the database, holding a pool connection for the duration."""
    global _inflight
    if _inflight + chaos["pool_pinned"] >= DB_POOL_MAX:
        # Pool is full: wait for a connection, then give up.
        await asyncio.sleep(POOL_WAIT_SECONDS)
        raise PoolExhausted("connection pool exhausted")
    _inflight += 1
    _publish_pool_gauge()
    try:
        delay = chaos["latency_ms"] / 1000
        if chaos["held"] is not None:
            delay += MEMORY_PRESSURE_DELAY      # memory pressure slows everything
        if delay:
            await asyncio.sleep(delay)
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            response = await client.get(f"{DOWNSTREAM_URL}{path}")
            response.raise_for_status()
            return response.json()
    finally:
        _inflight -= 1
        _publish_pool_gauge()


@app.get("/transactions")
async def transactions():
    if chaos["error_fraction"] and random.random() < chaos["error_fraction"]:
        transaction_errors_total.labels(SERVICE_NAME).inc()
        raise HTTPException(status_code=500, detail="injected fault")
    try:
        downstream = await call_downstream("/query")
    except (httpx.HTTPError, PoolExhausted) as exc:
        transaction_errors_total.labels(SERVICE_NAME).inc()
        raise HTTPException(status_code=503, detail=f"database unavailable: {exc}")
    transactions_total.labels(SERVICE_NAME).inc()
    return {"service": SERVICE_NAME, "downstream": downstream}


if CHAOS_ENABLED:

    @app.get("/chaos")
    async def chaos_state():
        held = chaos["held"]
        return {**chaos, "held": 0 if held is None else len(held) // (1024 * 1024)}

    @app.post("/chaos/pool")
    async def chaos_pool(active: int):
        """Pin `active` connections as checked out. Pool max is DB_POOL_MAX."""
        chaos["pool_pinned"] = max(0, active)
        _publish_pool_gauge()
        return {"pool_pinned": chaos["pool_pinned"], "pool_max": DB_POOL_MAX}

    @app.post("/chaos/errors")
    async def chaos_errors(fraction: float):
        chaos["error_fraction"] = min(1.0, max(0.0, fraction))
        return {"error_fraction": chaos["error_fraction"]}

    @app.post("/chaos/latency")
    async def chaos_latency(ms: int):
        chaos["latency_ms"] = max(0, ms)
        return {"latency_ms": chaos["latency_ms"]}

    @app.post("/chaos/memory")
    async def chaos_memory(mb: int):
        """Hold `mb` megabytes (replacing any earlier hold); 0 releases it."""
        chaos["held"] = None
        if mb > 0:
            chaos["held"] = bytearray(b"x") * (mb * 1024 * 1024)   # touches every page
        return {"held_mb": mb}

    @app.post("/chaos/reset")
    async def chaos_reset():
        chaos.update(pool_pinned=0, error_fraction=0.0, latency_ms=0, held=None)
        _publish_pool_gauge()
        return {"status": "reset"}


@app.get("/health")
async def health():
    return {"status": "ok", "service": SERVICE_NAME}


@app.get("/ready")
async def ready():
    return {"status": "ready", "service": SERVICE_NAME}
