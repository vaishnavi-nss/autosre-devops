"""AutoSRE - frontend service.

Position in the banking dependency chain (brief section 3a):

    frontend -> api-gateway -> transaction-service -> database
    ^^^^^^^^

This is the customer-facing entry point. It does no work of its own: it calls
the API gateway and returns whatever came back, so that a failure anywhere
below it surfaces here as a symptom.

Endpoints:
    GET /         business route - triggers a call down the chain
    GET /health   liveness probe
    GET /ready    readiness probe
    GET /metrics  Prometheus metrics
"""

import os
import time

import httpx
from fastapi import FastAPI, HTTPException, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

SERVICE_NAME = os.getenv("SERVICE_NAME", "frontend")
DOWNSTREAM_URL = os.getenv("DOWNSTREAM_URL", "http://localhost:8002")
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT_SECONDS", "5"))

# The `service` label is the contract with the correlation engine: every alert
# rule in monitoring/alert-rules.yaml carries it, and the engine uses it to
# find the service's position in the dependency chain.
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
    """Call the next service in the chain and return its JSON body."""
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        response = await client.get(f"{DOWNSTREAM_URL}{path}")
        response.raise_for_status()
        return response.json()


@app.get("/")
async def index():
    try:
        downstream = await call_downstream("/api/transfer")
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=503, detail=f"api-gateway unreachable: {exc}")
    return {"service": SERVICE_NAME, "downstream": downstream}


@app.get("/health")
async def health():
    return {"status": "ok", "service": SERVICE_NAME}


@app.get("/ready")
async def ready():
    return {"status": "ready", "service": SERVICE_NAME}
