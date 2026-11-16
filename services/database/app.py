import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel
from prometheus_client import Gauge, Counter, generate_latest, CONTENT_TYPE_LATEST, REGISTRY

app = FastAPI(title="database")

CHAOS_ENABLED = os.getenv("CHAOS_ENABLED", "false").lower() == "true"

# The disk the database reports on is a real, bounded directory: LOG_DIR (an emptyDir volume in Kubernetes). Its usage is
# the bytes of *.log files in it, as a share of DB_DISK_CAPACITY_MB, on top of a fixed baseline for the data itself. So a disk
# fault writes real bytes, and clearing the logs (the approved remediation) really frees them. If LOG_DIR does not exist or
# is not writable (a laptop, the unit tests) the service falls back to the old stored value, and says so in its replies.
FILL_PREFIX = "chaos-fill-"          # only files the fault itself created are ever removed by the service
MAX_FAULT_RATIO = 0.99


def log_dir() -> Path:
    return Path(os.getenv("LOG_DIR", "/var/log/app"))


def capacity_bytes() -> int:
    return int(float(os.getenv("DB_DISK_CAPACITY_MB", "40")) * 1024 * 1024)


def base_ratio() -> float:
    return float(os.getenv("DB_BASE_RATIO", "0.30"))


def real_disk_available() -> bool:
    d = log_dir()
    return d.is_dir() and os.access(d, os.W_OK)


def used_bytes() -> int:
    """Bytes of *.log files directly inside LOG_DIR (what the approved remediation deletes)."""
    d = log_dir()
    if not d.is_dir():
        return 0
    total = 0
    with os.scandir(d) as entries:
        for e in entries:
            if e.is_file(follow_symlinks=False) and e.name.endswith(".log"):
                try:
                    total += e.stat(follow_symlinks=False).st_size
                except OSError:
                    pass
    return total


_simulated = {"ratio": 0.30}


def disk_ratio() -> float:
    if real_disk_available():
        return round(base_ratio() + used_bytes() / capacity_bytes(), 4)
    return _simulated["ratio"]


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

# Evaluated at every scrape, so the gauge always reflects what is on disk right now.
DB_DISK_RATIO.labels(service="database").set_function(disk_ratio)


class DiskChaosRequest(BaseModel):
    ratio: float


def _chaos_files():
    d = log_dir()
    return sorted(d.glob(FILL_PREFIX + "*.log")) if d.is_dir() else []


def _remove_chaos_files() -> int:
    removed = 0
    for f in _chaos_files():
        if f.is_file() and not f.is_symlink():
            f.unlink()
            removed += 1
    return removed


def _fill(nbytes: int) -> int:
    """Write real bytes in one new file, 1 MiB at a time."""
    if nbytes <= 0:
        return 0
    path = log_dir() / f"{FILL_PREFIX}{len(_chaos_files()) + 1}.log"
    chunk = b"x" * (1024 * 1024)
    written = 0
    with open(path, "wb") as fh:
        while written < nbytes:
            n = min(len(chunk), nbytes - written)
            fh.write(chunk[:n])
            written += n
        fh.flush()
        os.fsync(fh.fileno())
    return written


@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

@app.get("/health")
def health():
    return {"status": "ok", "service": "database"}

@app.get("/ready")
def ready():
    return {"status": "ready", "service": "database"}

@app.get("/disk")
def disk():
    """What the disk metric is made of, for the scenario runner and for a human."""
    real = real_disk_available()
    return {"mode": "real" if real else "simulated", "ratio": disk_ratio(), "log_dir": str(log_dir()),
            "log_bytes": used_bytes(), "capacity_bytes": capacity_bytes(), "base_ratio": base_ratio(),
            "log_files": [f.name for f in log_dir().glob("*.log")] if real else []}

@app.post("/query")
def execute_query():
    HTTP_REQUESTS_TOTAL.labels(service="database", method="POST", status="200").inc()
    return {"database": "ok", "status": "committed"}

@app.post("/chaos/disk")
def chaos_disk(req: DiskChaosRequest):
    if not (os.getenv("CHAOS_ENABLED", "false").lower() == "true"):
        raise HTTPException(status_code=404, detail="Chaos disabled")
    if not 0.0 <= req.ratio <= MAX_FAULT_RATIO:
        raise HTTPException(status_code=422, detail=f"ratio must be between 0 and {MAX_FAULT_RATIO}")
    if not real_disk_available():
        _simulated["ratio"] = req.ratio
        return {"status": "disk ratio updated", "ratio": req.ratio, "mode": "simulated"}
    # Real mode: make the measured ratio equal the request by writing (or first removing) real bytes.
    _remove_chaos_files()
    other = used_bytes()                                    # any other *.log already there counts too
    want = max(0, int((req.ratio - base_ratio()) * capacity_bytes()) - other)
    written = _fill(want)
    return {"status": "disk ratio updated", "ratio": req.ratio, "measured_ratio": disk_ratio(), "mode": "real",
            "bytes_written": written}

@app.post("/chaos/reset")
def chaos_reset():
    if not (os.getenv("CHAOS_ENABLED", "false").lower() == "true"):
        raise HTTPException(status_code=404, detail="Chaos disabled")
    _simulated["ratio"] = 0.30
    removed = _remove_chaos_files() if real_disk_available() else 0
    return {"status": "database reset", "chaos_files_removed": removed}
