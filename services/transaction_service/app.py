import asyncio
import os
import time
import random
import httpx
from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel
from prometheus_client import Counter, Histogram, Gauge, generate_latest, CONTENT_TYPE_LATEST, REGISTRY

# 1. Instantiate app first
app = FastAPI(title="transaction-service")

CHAOS_ENABLED = os.getenv("CHAOS_ENABLED", "false").lower() == "true"
DATABASE_URL = os.getenv("DATABASE_URL", os.getenv("DOWNSTREAM_URL", "http://database.autosre.svc.cluster.local:8000"))
# The pool maximum the manifest sets (DB_POOL_MAX); the pool alert is active / this maximum above 0.9.
POOL_MAX = int(os.getenv("DB_POOL_MAX", "20"))
# The pool is real: a request holds one slot while it talks to the database and waits up to DB_POOL_TIMEOUT seconds for a free one. When
# every slot is taken (in-flight requests plus leaked ones) requests really time out with HTTP 503, which is what pool exhaustion does.
POOL_TIMEOUT = float(os.getenv("DB_POOL_TIMEOUT", "1.0"))
pool_state = {"inflight": 0, "leaked": 0, "pinned": 0}      # pinned: the older /chaos/pool gauge pin, kept for compatibility


def pool_active() -> int:
    """What the gauge reports: the slots really in use (in flight or leaked), or the pinned value if a test pinned a higher one."""
    return max(pool_state["inflight"] + pool_state["leaked"], pool_state["pinned"])


async def _acquire_slot() -> bool:
    deadline = time.time() + POOL_TIMEOUT
    while True:
        if pool_state["inflight"] + pool_state["leaked"] < POOL_MAX:
            pool_state["inflight"] += 1
            return True
        if time.time() >= deadline:
            return False
        await asyncio.sleep(0.05)

# 2. Safely get or register metrics to prevent DuplicateTimeseries in pytest
def get_or_create_gauge(name, documentation, labelnames):
    if name in REGISTRY._names_to_collectors:
        return REGISTRY._names_to_collectors[name]
    return Gauge(name, documentation, labelnames)

def get_or_create_counter(name, documentation, labelnames):
    if name in REGISTRY._names_to_collectors:
        return REGISTRY._names_to_collectors[name]
    return Counter(name, documentation, labelnames)

def get_or_create_histogram(name, documentation, labelnames):
    if name in REGISTRY._names_to_collectors:
        return REGISTRY._names_to_collectors[name]
    return Histogram(name, documentation, labelnames)

DB_POOL_ACTIVE = get_or_create_gauge(
    "db_connection_pool_active", 
    "Active DB connections in pool", 
    ["service"]
)
DB_POOL_MAX = get_or_create_gauge(
    "db_connection_pool_max",
    "Maximum DB connections in pool",
    ["service"]
)
HTTP_REQUESTS_TOTAL = get_or_create_counter(
    "http_requests_total", 
    "Total requests", 
    ["service", "method", "status"]
)
HTTP_LATENCY = get_or_create_histogram(
    "http_request_duration_seconds", 
    "Request latency", 
    ["service"]
)

DB_POOL_ACTIVE.labels(service="transaction-service").set_function(pool_active)       # evaluated at every scrape
DB_POOL_MAX.labels(service="transaction-service").set(POOL_MAX)

chaos_state = {
    "error_rate": 0.0,
    "latency_seconds": 0.0,
    "memory_leak": []
}

class PoolChaosRequest(BaseModel):
    active_connections: int


class LeakChaosRequest(BaseModel):
    connections: int

class ErrorChaosRequest(BaseModel):
    rate: float

class LatencyChaosRequest(BaseModel):
    seconds: float

class MemoryChaosRequest(BaseModel):
    megabytes: int

@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

@app.get("/health")
def health():
    return {"status": "ok", "service": "transaction-service"}

@app.post("/transaction")
async def execute_transaction():
    start_time = time.time()
    
    if chaos_state["latency_seconds"] > 0:
        time.sleep(chaos_state["latency_seconds"])

    if chaos_state["error_rate"] > 0 and random.random() < chaos_state["error_rate"]:
        HTTP_REQUESTS_TOTAL.labels(service="transaction-service", method="POST", status="500").inc()
        HTTP_LATENCY.labels(service="transaction-service").observe(time.time() - start_time)
        raise HTTPException(status_code=500, detail="Injected transaction failure")

    if not await _acquire_slot():
        HTTP_REQUESTS_TOTAL.labels(service="transaction-service", method="POST", status="503").inc()
        HTTP_LATENCY.labels(service="transaction-service").observe(time.time() - start_time)
        raise HTTPException(status_code=503, detail="Connection pool exhausted")
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.post(f"{DATABASE_URL}/query", json={"action": "commit"})
            db_status = resp.status_code
    except Exception:
        db_status = 503
    finally:
        pool_state["inflight"] -= 1

    status_code = "200" if db_status == 200 else str(db_status)
    HTTP_REQUESTS_TOTAL.labels(service="transaction-service", method="POST", status=status_code).inc()
    HTTP_LATENCY.labels(service="transaction-service").observe(time.time() - start_time)

    if status_code != "200":
        raise HTTPException(status_code=502, detail="Database upstream failure")

    return {"status": "success", "service": "transaction-service"}

# Chaos Endpoints
@app.post("/chaos/pool")
def chaos_pool(req: PoolChaosRequest):
    # Dynamic check so monkeypatch works without re-importing
    if not (os.getenv("CHAOS_ENABLED", "false").lower() == "true"):
        raise HTTPException(status_code=404, detail="Chaos disabled")
    pool_state["pinned"] = req.active_connections
    return {"status": "pool set", "value": req.active_connections}


@app.post("/chaos/leak")
def chaos_leak(req: LeakChaosRequest):
    """Leak pool slots: they are taken and never returned, until /chaos/reset or a restart (a new process starts with a full pool).
    With every slot leaked, requests really time out after DB_POOL_TIMEOUT and fail with 503."""
    if not (os.getenv("CHAOS_ENABLED", "false").lower() == "true"):
        raise HTTPException(status_code=404, detail="Chaos disabled")
    pool_state["leaked"] = max(0, min(req.connections, POOL_MAX))
    return {"status": "slots leaked", "leaked": pool_state["leaked"], "pool_max": POOL_MAX}

@app.post("/chaos/errors")
def chaos_errors(req: ErrorChaosRequest):
    if not (os.getenv("CHAOS_ENABLED", "false").lower() == "true"):
        raise HTTPException(status_code=404, detail="Chaos disabled")
    chaos_state["error_rate"] = req.rate
    return {"status": "error rate set", "value": req.rate}

@app.post("/chaos/latency")
def chaos_latency(req: LatencyChaosRequest):
    if not (os.getenv("CHAOS_ENABLED", "false").lower() == "true"):
        raise HTTPException(status_code=404, detail="Chaos disabled")
    chaos_state["latency_seconds"] = req.seconds
    return {"status": "latency set", "value": req.seconds}

@app.post("/chaos/memory")
def chaos_memory(req: MemoryChaosRequest):
    if not (os.getenv("CHAOS_ENABLED", "false").lower() == "true"):
        raise HTTPException(status_code=404, detail="Chaos disabled")
    block = bytearray(req.megabytes * 1024 * 1024)
    chaos_state["memory_leak"].append(block)
    return {"status": "memory allocated", "mb": req.megabytes}

@app.post("/chaos/reset")
def chaos_reset():
    if not (os.getenv("CHAOS_ENABLED", "false").lower() == "true"):
        raise HTTPException(status_code=404, detail="Chaos disabled")
    chaos_state["error_rate"] = 0.0
    chaos_state["latency_seconds"] = 0.0
    chaos_state["memory_leak"].clear()
    pool_state["pinned"] = 0
    pool_state["leaked"] = 0
    DB_POOL_MAX.labels(service="transaction-service").set(POOL_MAX)
    return {"status": "chaos reset"}
