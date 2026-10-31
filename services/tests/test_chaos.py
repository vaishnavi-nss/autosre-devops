"""Tests for the fault-injection hooks on the services.

Run from the repo root:   pytest services/tests

The faults only matter if the monitoring pipeline sees what a real fault would
produce, so these check the *effects* (status codes, the pool gauge) and not just
that the endpoints respond. They also check the safety property that matters
most: with CHAOS_ENABLED unset, none of the routes exist.
"""

import importlib.util
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

SERVICES = Path(__file__).resolve().parents[1]


def load(service: str, **env):
    """Import services/<service>/app.py fresh, with the given environment.

    Every service registers metrics with the same names in the global registry,
    so the registry is emptied first - otherwise the second import in a process
    fails with "Duplicated timeseries".
    """
    for collector in list(REGISTRY._collector_to_names):
        REGISTRY.unregister(collector)

    keys = {"CHAOS_ENABLED", "POOL_WAIT_SECONDS", "DISK_STALL_SECONDS", "DB_POOL_MAX"} | set(env)
    saved = {k: os.environ.get(k) for k in keys}
    for k in keys:
        os.environ.pop(k, None)
    os.environ.update({k: str(v) for k, v in env.items()})
    try:
        spec = importlib.util.spec_from_file_location(
            f"svc_{service.replace('-', '_')}", SERVICES / service / "app.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def gauge(name: str, service: str) -> float:
    return REGISTRY.get_sample_value(name, {"service": service})


# -- the safety property ---------------------------------------------------------------

@pytest.mark.parametrize(
    "service, route",
    [
        ("transaction-service", "/chaos/pool?active=5"),
        ("transaction-service", "/chaos/memory?mb=1"),
        ("api-gateway", "/chaos/errors?fraction=1"),
        ("database", "/chaos/disk?ratio=0.99"),
    ],
)
def test_chaos_routes_do_not_exist_unless_enabled(service, route):
    client = TestClient(load(service).app)
    assert client.post(route).status_code == 404
    assert client.get("/chaos").status_code == 404


# -- transaction-service ---------------------------------------------------------------

@pytest.fixture
def tx():
    module = load("transaction-service", CHAOS_ENABLED="true", POOL_WAIT_SECONDS="0.01", DB_POOL_MAX="20")
    return module, TestClient(module.app)


def test_pinning_the_pool_raises_the_gauge_above_the_alert_threshold(tx):
    module, client = tx
    client.post("/chaos/pool?active=19")
    # The DBPoolNearExhaustion rule is  active / max > 0.9
    assert gauge("db_connection_pool_active", "transaction-service") / 20 > 0.9


def test_a_full_pool_rejects_requests_so_errors_and_latency_follow(tx):
    module, client = tx
    client.post("/chaos/pool?active=20")
    response = client.get("/transactions")
    assert response.status_code == 503
    assert "pool exhausted" in response.text
    assert gauge("transaction_errors_total", "transaction-service") >= 1


def test_a_nearly_full_pool_is_not_rejected_by_the_pool_check(tx):
    # 19 of 20 pinned leaves room for the request's own connection, so it gets as
    # far as calling the database (which is not running here -> 503 from that).
    module, client = tx
    client.post("/chaos/pool?active=19")
    response = client.get("/transactions")
    assert "pool exhausted" not in response.text


def test_reset_clears_every_fault(tx):
    module, client = tx
    client.post("/chaos/pool?active=20")
    client.post("/chaos/errors?fraction=1")
    client.post("/chaos/latency?ms=500")
    client.post("/chaos/memory?mb=2")
    client.post("/chaos/reset")
    state = client.get("/chaos").json()
    assert state == {"pool_pinned": 0, "error_fraction": 0.0, "latency_ms": 0, "held": 0}
    assert gauge("db_connection_pool_active", "transaction-service") == 0


def test_injected_errors_are_500s(tx):
    module, client = tx
    client.post("/chaos/errors?fraction=1")
    assert client.get("/transactions").status_code == 500


def test_memory_hold_is_reported_and_releasable(tx):
    module, client = tx
    client.post("/chaos/memory?mb=3")
    assert client.get("/chaos").json()["held"] == 3
    client.post("/chaos/memory?mb=0")
    assert client.get("/chaos").json()["held"] == 0


def test_fraction_is_clamped(tx):
    module, client = tx
    assert client.post("/chaos/errors?fraction=7").json()["error_fraction"] == 1.0
    assert client.post("/chaos/errors?fraction=-3").json()["error_fraction"] == 0.0


# -- api-gateway -----------------------------------------------------------------------

def test_gateway_injected_errors_and_reset():
    client = TestClient(load("api-gateway", CHAOS_ENABLED="true").app)
    client.post("/chaos/errors?fraction=1")
    assert client.get("/api/transfer").status_code == 500
    client.post("/chaos/reset")
    assert client.get("/chaos").json() == {"error_fraction": 0.0, "latency_ms": 0}


# -- database --------------------------------------------------------------------------

@pytest.fixture
def db():
    module = load("database", CHAOS_ENABLED="true", DISK_STALL_SECONDS="0.01")
    return module, TestClient(module.app)


def test_nearly_full_disk_raises_only_the_gauge(db):
    module, client = db
    client.post("/chaos/disk?ratio=0.95")
    assert gauge("db_disk_usage_ratio", "database") == 0.95
    assert client.get("/query").status_code == 200       # still serving


def test_a_full_disk_fails_queries_with_507(db):
    module, client = db
    client.post("/chaos/disk?ratio=0.99")
    assert client.get("/query").status_code == 507


def test_disk_reset_restores_the_baseline(db):
    module, client = db
    client.post("/chaos/disk?ratio=0.99")
    client.post("/chaos/reset")
    assert client.get("/query").status_code == 200
    assert gauge("db_disk_usage_ratio", "database") == 0.35
