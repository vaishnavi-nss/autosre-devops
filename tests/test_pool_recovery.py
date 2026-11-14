import importlib
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

tx = importlib.import_module("services.transaction_service.app")


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("CHAOS_ENABLED", "false")
    monkeypatch.setenv("POOL_FAULT_ENABLED", "true")
    monkeypatch.setattr(tx, "pool_active", 2)
    with TestClient(tx.app) as test:
        yield test


def test_pool_hook_causes_real_transaction_failure_and_reset_recovers(client, monkeypatch):
    assert client.post("/chaos/pool", json={"active_connections": tx.POOL_MAX}).status_code == 200
    assert client.post("/transaction").status_code == 503
    assert client.get("/recovery").json()["pool_exhausted"] is True
    # Other fault hooks remain disabled despite the approved pool hook.
    assert client.post("/chaos/memory", json={"megabytes": 10}).status_code == 404
    assert client.post("/chaos/errors", json={"rate": 1}).status_code == 404
    # Process startup after restart resets the same in-memory state to 2.
    tx.pool_active = 2
    tx.DB_POOL_ACTIVE.labels(service="transaction-service").set(2)
    monkeypatch.setattr(httpx.AsyncClient, "post", AsyncMock(return_value=httpx.Response(200)))
    assert client.post("/transaction").status_code == 200
    assert client.get("/recovery").json()["pool_exhausted"] is False


def test_pool_fault_is_bounded_and_disabled_by_default(client, monkeypatch):
    assert client.post("/chaos/pool", json={"active_connections": -1}).status_code == 422
    assert client.post("/chaos/pool", json={"active_connections": tx.POOL_MAX + 1}).status_code == 422
    monkeypatch.delenv("POOL_FAULT_ENABLED")
    assert client.post("/chaos/pool", json={"active_connections": tx.POOL_MAX}).status_code == 404
