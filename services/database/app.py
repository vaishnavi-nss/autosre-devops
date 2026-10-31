"""AutoSRE - mock database service.

Position in the banking dependency chain (brief section 3a):

    frontend -> api-gateway -> transaction-service -> database
                                                      ^^^^^^^^

This is a stand-in, not a real database engine. It is the deepest service in
the chain, which makes it the most likely root cause when everything above it
is alerting at once.

`db_disk_usage_ratio` backs the `db_disk_full` root cause from brief section 6.

Endpoints:
    GET /query    business route - returns a fake result set
    GET /health   liveness probe
    GET /ready    readiness probe
    GET /metrics  Prometheus metrics
"""

import asyncio
import os
import time

from fastapi import FastAPI, HTTPException, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

SERVICE_NAME = os.getenv("SERVICE_NAME", "database")
DISK_USAGE_RATIO = float(os.getenv("DB_DISK_USAGE_RATIO", "0.35"))

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
db_queries_total = Counter("db_queries_total", "Queries executed.", ["service"])
db_disk_usage_ratio = Gauge(
    "db_disk_usage_ratio",
    "Fraction of the data volume in use, 0.0 to 1.0.",
    ["service"],
)

db_disk_usage_ratio.labels(SERVICE_NAME).set(DISK_USAGE_RATIO)

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
# Routes exist only when CHAOS_ENABLED=true. A volume that is nearly full only
# raises the disk gauge; one that is effectively full stalls and then fails
# queries with 507, which is how the fault reaches the services above it.
CHAOS_ENABLED = os.getenv("CHAOS_ENABLED", "false").lower() == "true"
DISK_FULL_THRESHOLD = 0.98
DISK_STALL_SECONDS = float(os.getenv("DISK_STALL_SECONDS", "0.6"))
chaos = {"disk_ratio": DISK_USAGE_RATIO}


@app.get("/query")
async def query():
    if chaos["disk_ratio"] >= DISK_FULL_THRESHOLD:
        await asyncio.sleep(DISK_STALL_SECONDS)
        raise HTTPException(status_code=507, detail="no space left on device")
    db_queries_total.labels(SERVICE_NAME).inc()
    return {
        "service": SERVICE_NAME,
        "rows": [{"account": "ACC-1001", "balance": 4250.75, "currency": "INR"}],
    }


if CHAOS_ENABLED:

    @app.get("/chaos")
    async def chaos_state():
        return chaos

    @app.post("/chaos/disk")
    async def chaos_disk(ratio: float):
        """Set how full the data volume is, 0.0 to 1.0."""
        chaos["disk_ratio"] = min(1.0, max(0.0, ratio))
        db_disk_usage_ratio.labels(SERVICE_NAME).set(chaos["disk_ratio"])
        return chaos

    @app.post("/chaos/reset")
    async def chaos_reset():
        chaos["disk_ratio"] = DISK_USAGE_RATIO
        db_disk_usage_ratio.labels(SERVICE_NAME).set(DISK_USAGE_RATIO)
        return {"status": "reset"}


@app.get("/health")
async def health():
    return {"status": "ok", "service": SERVICE_NAME}


@app.get("/ready")
async def ready():
    return {"status": "ready", "service": SERVICE_NAME}
