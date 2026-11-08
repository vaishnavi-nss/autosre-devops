"""AutoSRE second opinion - checks the rule engine's diagnosis against raw metrics.

The correlation engine reasons over alerts. This service looks at the metrics underneath
them: it pulls the incident window from Prometheus, scores each service the way the
RCAEval-trained model was trained, and says whether it agrees on where the fault is (and,
for memory pressure, what kind). It never acts on anything; the engine decides what to do
with the verdict.

Endpoints:
    POST /opinion   {root_cause, service, incident_start}  ->  verdict
    GET  /health    liveness
    GET  /ready     readiness (model loaded)
    GET  /metrics   Prometheus metrics

Environment:
    PROMETHEUS_URL      default http://monitoring-kube-prometheus-prometheus.monitoring:9090
    MODEL_PATH          default models/second_opinion.joblib
    MIN_CONFIDENCE      below this the scorer abstains (default 0.3; precision on held-out systems is flat from 0.3 to 0.6)
    BASELINE_MINUTES    how much earlier behaviour to compare against (default 30)
    LEAD_SECONDS        alerts fire after the scrape interval and their `for:` delay (30-60s),
                        so the fault is assumed to have started this long before the earliest
                        alert (default 60). Too large and normal samples dilute the window.
"""

import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

import joblib
from fastapi import FastAPI, HTTPException, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, generate_latest

from opinion import judge
from prometheus import fetch_metrics
from scoring import KINDS, OWN_KINDS, service_scores

SERVICE_NAME = os.getenv("SERVICE_NAME", "second-opinion")
PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://monitoring-kube-prometheus-prometheus.monitoring:9090")
MODEL_PATH = Path(os.getenv("MODEL_PATH", Path(__file__).resolve().parent / "models" / "second_opinion.joblib"))
MIN_CONFIDENCE = float(os.getenv("MIN_CONFIDENCE", "0.3"))
BASELINE_S = int(float(os.getenv("BASELINE_MINUTES", "30")) * 60)
LEAD_S = int(os.getenv("LEAD_SECONDS", "60"))
MIN_FAULT_SAMPLES = 4
MIN_BASELINE_SAMPLES = 20           # five minutes at a 15s step
# The chain the engine reasons about (correlation-engine/dependency_graph.py).
SERVICES = ("frontend", "api-gateway", "transaction-service", "database")

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("second-opinion")

opinions_total = Counter("second_opinion_verdicts_total", "Verdicts returned.", ["service", "verdict"])

model = None


@asynccontextmanager
async def lifespan(_app):
    global model
    try:
        model = joblib.load(MODEL_PATH)
        log.info("loaded scorer from %s", MODEL_PATH)
    except Exception as exc:                      # the engine treats a dead scorer as no opinion
        log.error("could not load scorer from %s: %s", MODEL_PATH, exc)
    yield


app = FastAPI(title="AutoSRE second opinion", lifespan=lifespan)


def window_scores(incident_start: float, kinds, now: float | None = None):
    """Score the incident window. Returns (scores, None) or (None, reason)."""
    now = now or time.time()
    fault_start = int(incident_start) - LEAD_S
    frame = fetch_metrics(PROMETHEUS_URL, SERVICES, fault_start - BASELINE_S, now, kinds=kinds)
    if frame.empty:
        return None, "no metrics returned by Prometheus"
    fault_samples = int(((frame.time >= fault_start)).sum())
    if fault_samples < MIN_FAULT_SAMPLES:
        return None, f"only {fault_samples} samples since the incident began"
    baseline_samples = int((frame.time < fault_start).sum())
    if baseline_samples < MIN_BASELINE_SAMPLES:
        return None, f"only {baseline_samples} baseline samples before the incident"
    scores = service_scores(frame, fault_start, int(now) - fault_start + 1, kinds)
    if len(scores) < 2:
        return None, f"metrics found for only {len(scores)} service(s)"
    return scores, None


def opinion_for(cause: str, service: str, incident_start: float, now: float | None = None) -> dict:
    """Score the incident window and judge the rule engine's diagnosis against it."""
    scores, reason = window_scores(incident_start, getattr(model, "kinds", KINDS), now)
    if scores is None:
        return {"verdict": "no_opinion", "reason": reason}
    scorer_service, scorer_fault, confidence, ranking = model.diagnose(scores)
    result = judge(cause, service, scorer_service, scorer_fault, confidence, MIN_CONFIDENCE,
                   vocabulary=set(map(str, model.fault_model.classes_)))
    result["ranking"] = [{"service": s, "score": round(p, 3)} for s, p in ranking]
    return result


@app.post("/opinion")
def opinion(body: dict):
    """Sync on purpose: the Prometheus calls block, so FastAPI runs this in its thread pool."""
    try:
        cause, service, start = body["root_cause"], body["service"], float(body["incident_start"])
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"need root_cause, service, incident_start: {exc}")
    if model is None:
        raise HTTPException(status_code=503, detail="scorer not loaded")
    try:
        result = opinion_for(cause, service, start)
    except Exception as exc:                      # Prometheus down, bad data, ...
        log.warning("opinion failed: %s", exc)
        result = {"verdict": "no_opinion", "reason": f"could not score: {exc}"}
    opinions_total.labels(service, result["verdict"]).inc()
    log.info("%s on %s: %s (%s)", cause, service, result["verdict"], result.get("reason"))
    return result


@app.post("/features")
def features(body: dict):
    """The scores the model is given, for every signal we can measure. The live harness records
    these so a model can be trained on this system's own faults (train_own.py)."""
    try:
        start = float(body["incident_start"])
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"need incident_start: {exc}")
    try:
        scores, reason = window_scores(start, OWN_KINDS)
    except Exception as exc:                      # Prometheus down, bad data, ...
        scores, reason = None, f"could not score: {exc}"
    return {"scores": scores} if scores is not None else {"scores": None, "reason": reason}


@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/health")
def health():
    return {"status": "ok", "service": SERVICE_NAME}


@app.get("/ready")
def ready():
    if model is None:
        raise HTTPException(status_code=503, detail="scorer not loaded")
    return {"status": "ready", "service": SERVICE_NAME}
