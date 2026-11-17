"""The pool and the disk faults are real now: a leaked connection pool really times requests out, and a full disk really fails writes."""
import re

import pytest
from fastapi.testclient import TestClient

from services.database import app as db
from services.transaction_service import app as tx

tx_client = TestClient(tx.app)
db_client = TestClient(db.app)


def scraped(client, metric, labels):
    text = client.get("/metrics").text
    return float(re.search(rf"^{metric}\{{{labels}\}} ([0-9.e+-]+)$", text, re.M).group(1))


class OkDatabase:
    """The database answers 200 (or the status we choose)."""
    status = 200

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json):
        return type("R", (), {"status_code": OkDatabase.status})()


@pytest.fixture
def pool(monkeypatch):
    monkeypatch.setenv("CHAOS_ENABLED", "true")
    monkeypatch.setattr(tx, "POOL_TIMEOUT", 0.1)
    monkeypatch.setattr(tx.httpx, "AsyncClient", OkDatabase)
    OkDatabase.status = 200
    tx_client.post("/chaos/reset")
    yield
    tx_client.post("/chaos/reset")


def pool_active():
    return scraped(tx_client, "db_connection_pool_active", 'service="transaction-service"')


def test_a_healthy_pool_serves_requests_and_returns_every_slot(pool):
    assert tx_client.post("/transaction").status_code == 200
    assert tx.pool_state["inflight"] == 0 and pool_active() == 0


def test_leaking_every_slot_makes_requests_really_time_out_with_503(pool):
    assert tx_client.post("/chaos/leak", json={"connections": tx.POOL_MAX}).json()["leaked"] == tx.POOL_MAX
    assert pool_active() == tx.POOL_MAX and pool_active() / tx.POOL_MAX > 0.9           # the alert condition, measured, not set
    r = tx_client.post("/transaction")
    assert r.status_code == 503 and "pool exhausted" in r.json()["detail"].lower()
    assert scraped(tx_client, "http_requests_total", 'method="POST",service="transaction-service",status="503"') >= 1
    assert tx.pool_state["inflight"] == 0                                                 # a failed wait does not hold a slot


def test_a_partial_leak_leaves_the_service_working_but_raises_the_gauge(pool):
    tx_client.post("/chaos/leak", json={"connections": tx.POOL_MAX - 1})
    assert pool_active() == tx.POOL_MAX - 1
    assert tx_client.post("/transaction").status_code == 200


def test_reset_gives_the_slots_back(pool):
    tx_client.post("/chaos/leak", json={"connections": tx.POOL_MAX})
    assert tx_client.post("/transaction").status_code == 503
    tx_client.post("/chaos/reset")
    assert pool_active() == 0 and tx_client.post("/transaction").status_code == 200


def test_a_request_releases_its_slot_even_when_the_database_fails(pool):
    OkDatabase.status = 507
    assert tx_client.post("/transaction").status_code == 502
    assert tx.pool_state["inflight"] == 0


def test_the_leak_is_capped_and_chaos_stays_off_by_default(pool, monkeypatch):
    assert tx_client.post("/chaos/leak", json={"connections": 10_000}).json()["leaked"] == tx.POOL_MAX
    monkeypatch.delenv("CHAOS_ENABLED")
    assert tx_client.post("/chaos/leak", json={"connections": 1}).status_code == 404


def test_the_older_pool_pin_still_works_for_compatibility(pool):
    tx_client.post("/chaos/pool", json={"active_connections": 19})
    assert pool_active() == 19
    tx_client.post("/chaos/reset")
    assert pool_active() == 0


# ------------------------------------------------------------------------------------------------ a full disk fails writes
@pytest.fixture
def disk(monkeypatch, tmp_path):
    monkeypatch.setenv("CHAOS_ENABLED", "true")
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    monkeypatch.setenv("DB_DISK_CAPACITY_MB", "4")
    monkeypatch.setenv("DB_BASE_RATIO", "0.30")
    return tmp_path


def test_a_full_disk_fails_writes_with_507_and_clearing_the_logs_fixes_them(disk):
    assert db_client.post("/query").status_code == 200
    db_client.post("/chaos/disk", json={"ratio": 0.97})
    r = db_client.post("/query")
    assert r.status_code == 507 and "disk is full" in r.json()["detail"]
    assert scraped(db_client, "http_requests_total", 'method="POST",service="database",status="507"') >= 1
    for f in disk.glob("*.log"):                       # what clear_logs.yml does
        f.unlink()
    assert db_client.post("/query").status_code == 200


def test_a_nearly_full_disk_alerts_but_still_writes(disk):
    db_client.post("/chaos/disk", json={"ratio": 0.92})                 # above the 0.9 alert, below the 0.95 failure point
    assert db_client.post("/query").status_code == 200


def test_without_a_real_log_directory_writes_never_fail(monkeypatch, tmp_path):
    monkeypatch.setenv("CHAOS_ENABLED", "true")
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "missing"))
    db_client.post("/chaos/disk", json={"ratio": 0.99})
    assert db_client.post("/query").status_code == 200
    db_client.post("/chaos/reset")


# ---------------------------------------------------------------- memory: the slowness is real work on the leaked heap, not a sleep
def test_leaked_memory_makes_every_request_really_do_work_and_reset_clears_it(pool, monkeypatch):
    scans = []
    real = tx._scan_leaked_memory
    monkeypatch.setattr(tx, "_scan_leaked_memory", lambda passes: scans.append(passes) or real(passes))
    assert tx_client.post("/transaction").status_code == 200 and scans == []          # no leak, no extra work
    r = tx_client.post("/chaos/memory", json={"megabytes": 8}).json()
    assert r["held_mb"] == 8 and r["scan_passes"] == tx.DEFAULT_SCAN_PASSES
    assert tx_client.post("/transaction").status_code == 200
    assert scans == [tx.DEFAULT_SCAN_PASSES]                                          # the request read the heap
    assert tx.chaos_state["latency_seconds"] == 0.0                                    # and nothing was slept
    tx_client.post("/chaos/reset")
    assert tx.chaos_state["memory_leak"] == [] and tx.chaos_state["scan_passes"] == 0
    tx_client.post("/transaction")
    assert scans == [tx.DEFAULT_SCAN_PASSES]


def test_scan_passes_can_be_tuned_without_allocating_more_and_bad_values_are_rejected(pool):
    tx_client.post("/chaos/memory", json={"megabytes": 4})
    r = tx_client.post("/chaos/memory", json={"megabytes": 0, "scan_passes": 9}).json()
    assert r["held_mb"] == 4 and r["scan_passes"] == 9
    assert tx_client.post("/chaos/memory", json={"megabytes": -1}).status_code == 422
    assert tx_client.post("/chaos/memory", json={"megabytes": 0, "scan_passes": 999}).status_code == 422


def test_the_scan_really_reads_every_byte_of_the_leak():
    import zlib
    tx.chaos_state["memory_leak"].clear()
    tx.chaos_state["memory_leak"].append(bytearray(b"ab" * 1024))
    assert tx._scan_leaked_memory(1) == zlib.crc32(b"ab" * 1024)
    assert tx._scan_leaked_memory(2) == zlib.crc32(b"ab" * 1024, zlib.crc32(b"ab" * 1024))
    tx.chaos_state["memory_leak"].clear()
