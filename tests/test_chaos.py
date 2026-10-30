import pytest
import os
from fastapi.testclient import TestClient
from services.transaction_service.app import app as tx_app
from services.database.app import app as db_app

tx_client = TestClient(tx_app)
db_client = TestClient(db_app)

def test_transaction_chaos_disabled_by_default(monkeypatch):
    monkeypatch.delenv("CHAOS_ENABLED", raising=False)
    res = tx_client.post("/chaos/pool", json={"active_connections": 19})
    assert res.status_code == 404

def test_transaction_chaos_pool_injection(monkeypatch):
    monkeypatch.setenv("CHAOS_ENABLED", "true")
    res = tx_client.post("/chaos/pool", json={"active_connections": 19})
    assert res.status_code == 200
    assert res.json()["value"] == 19

def test_database_chaos_disk_disabled_by_default(monkeypatch):
    monkeypatch.delenv("CHAOS_ENABLED", raising=False)
    res = db_client.post("/chaos/disk", json={"ratio": 0.95})
    assert res.status_code == 404

def test_database_chaos_disk_injection(monkeypatch):
    monkeypatch.setenv("CHAOS_ENABLED", "true")
    res = db_client.post("/chaos/disk", json={"ratio": 0.95})
    assert res.status_code == 200
    assert res.json()["ratio"] == 0.95
