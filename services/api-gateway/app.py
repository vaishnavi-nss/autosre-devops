import os
import time
import httpx
from fastapi import FastAPI, HTTPException, Response
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST, REGISTRY

app = FastAPI(title="api-gateway")

TRANSACTION_SERVICE_URL = os.getenv("TRANSACTION_SERVICE_URL", "http://transaction-service.autosre.svc.cluster.local:8000")

def get_or_create_counter(name, documentation, labelnames):
    if name in REGISTRY._names_to_collectors:
        return REGISTRY._names_to_collectors[name]
    return Counter(name, documentation, labelnames)

def get_or_create_histogram(name, documentation, labelnames):
    if name in REGISTRY._names_to_collectors:
        return REGISTRY._names_to_collectors[name]
    return Histogram(name, documentation, labelnames)

HTTP_REQUESTS_TOTAL = get_or_create_counter(
    "http_requests_total", "Total incoming requests", ["service", "method", "status"]
)
HTTP_LATENCY = get_or_create_histogram(
    "http_request_duration_seconds", "Request latency duration", ["service"]
)

@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

@app.get("/health")
def health():
    return {"status": "ok", "service": "api-gateway"}

@app.get("/api/v1/trade")
async def execute_trade():
    start_time = time.time()
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.post(f"{TRANSACTION_SERVICE_URL}/transaction")
            status_code = str(resp.status_code)
    except Exception:
        status_code = "503"

    HTTP_REQUESTS_TOTAL.labels(service="api-gateway", method="GET", status=status_code).inc()
    HTTP_LATENCY.labels(service="api-gateway").observe(time.time() - start_time)

    if status_code != "200":
        raise HTTPException(status_code=int(status_code), detail="Downstream transaction failed")

    return {"status": "success", "service": "api-gateway"}
