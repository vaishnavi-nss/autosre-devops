import os
from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel
from prometheus_client import Gauge, Counter, generate_latest, CONTENT_TYPE_LATEST, REGISTRY

app = FastAPI(title="database")

CHAOS_ENABLED = os.getenv("CHAOS_ENABLED", "false").lower() == "true"

def get_or_create_gauge(name, documentation, labelnames):
    if name in REGISTRY._names_to_collectors:
        return REGISTRY._names_to_collectors[name]
    return Gauge(name, documentation, labelnames)

def get_or_create_counter(name, documentation, labelnames):
    if name in REGISTRY._names_to_collectors:
        return REGISTRY._names_to_collectors[name]
    return Counter(name, documentation, labelnames)

DB_DISK_RATIO = get_or_create_gauge(
    "db_disk_usage_ratio",
    "Disk usage ratio for database storage",
    ["service"]
)
HTTP_REQUESTS_TOTAL = get_or_create_counter(
    "http_requests_total",
    "Total requests",
    ["service", "method", "status"]
)

# Baseline ratio at 30%
DB_DISK_RATIO.labels(service="database").set(0.30)

class DiskChaosRequest(BaseModel):
    ratio: float

@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

@app.get("/health")
def health():
    return {"status": "ok", "service": "database"}

@app.post("/query")
def execute_query():
    HTTP_REQUESTS_TOTAL.labels(service="database", method="POST", status="200").inc()
    return {"database": "ok", "status": "committed"}

@app.post("/chaos/disk")
def chaos_disk(req: DiskChaosRequest):
    if not (os.getenv("CHAOS_ENABLED", "false").lower() == "true"):
        raise HTTPException(status_code=404, detail="Chaos disabled")
    DB_DISK_RATIO.labels(service="database").set(req.ratio)
    return {"status": "disk ratio updated", "ratio": req.ratio}

@app.post("/chaos/reset")
def chaos_reset():
    if not (os.getenv("CHAOS_ENABLED", "false").lower() == "true"):
        raise HTTPException(status_code=404, detail="Chaos disabled")
    DB_DISK_RATIO.labels(service="database").set(0.30)
    return {"status": "database reset"}
