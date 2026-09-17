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

import os
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


async def call_downstream(path: str) -> dict:
    """Call the database, holding a pool connection for the duration."""
    db_connection_pool_active.labels(SERVICE_NAME).inc()
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            response = await client.get(f"{DOWNSTREAM_URL}{path}")
            response.raise_for_status()
            return response.json()
    finally:
        db_connection_pool_active.labels(SERVICE_NAME).dec()


@app.get("/transactions")
async def transactions():
    try:
        downstream = await call_downstream("/query")
    except httpx.HTTPError as exc:
        transaction_errors_total.labels(SERVICE_NAME).inc()
        raise HTTPException(status_code=503, detail=f"database unreachable: {exc}")
    transactions_total.labels(SERVICE_NAME).inc()
    return {"service": SERVICE_NAME, "downstream": downstream}


@app.get("/health")
async def health():
    return {"status": "ok", "service": SERVICE_NAME}


@app.get("/ready")
async def ready():
    return {"status": "ready", "service": SERVICE_NAME}
