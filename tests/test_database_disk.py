"""The database disk fault is real: it writes bytes into a bounded log directory, the metric measures them, and the approved
cleanup (delete *.log in that directory) brings the metric back down. With no such directory it falls back to a stored value."""
import re
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from services.database import app as db

client = TestClient(db.app)
ROOT = Path(__file__).resolve().parents[1]


def scraped_ratio() -> float:
    text = client.get("/metrics").text
    return float(re.search(r'^db_disk_usage_ratio\{service="database"\} ([0-9.e+-]+)$', text, re.M).group(1))


@pytest.fixture
def real(monkeypatch, tmp_path):
    monkeypatch.setenv("CHAOS_ENABLED", "true")
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    monkeypatch.setenv("DB_DISK_CAPACITY_MB", "4")          # small, so the tests write a few MiB, not tens
    monkeypatch.setenv("DB_BASE_RATIO", "0.30")
    return tmp_path


def test_a_disk_fault_writes_real_bytes_and_the_metric_measures_them(real):
    assert scraped_ratio() == pytest.approx(0.30)
    r = client.post("/chaos/disk", json={"ratio": 0.95}).json()
    assert r["mode"] == "real" and r["ratio"] == 0.95 and r["measured_ratio"] == pytest.approx(0.95, abs=0.01)
    files = list(real.glob("chaos-fill-*.log"))
    assert files and sum(f.stat().st_size for f in files) == pytest.approx(0.65 * 4 * 1024 * 1024, abs=1024)
    assert scraped_ratio() == pytest.approx(0.95, abs=0.01)


def test_setting_a_new_level_replaces_the_old_one(real):
    client.post("/chaos/disk", json={"ratio": 0.95})
    client.post("/chaos/disk", json={"ratio": 0.5})
    assert scraped_ratio() == pytest.approx(0.5, abs=0.01)
    client.post("/chaos/disk", json={"ratio": 0.95})
    assert scraped_ratio() == pytest.approx(0.95, abs=0.01) and len(list(real.glob("chaos-fill-*.log"))) == 1


def test_the_approved_cleanup_really_frees_the_space(real):
    client.post("/chaos/disk", json={"ratio": 0.95})
    for f in real.glob("*.log"):                    # exactly what clear_logs.yml does: *.log directly inside the directory
        f.unlink()
    assert scraped_ratio() == pytest.approx(0.30)
    assert client.get("/disk").json()["log_bytes"] == 0


def test_reset_removes_only_what_the_fault_wrote(real):
    (real / "data.txt").write_text("not a log")
    (real / "application.log").write_text("real log line\n")
    client.post("/chaos/disk", json={"ratio": 0.95})
    out = client.post("/chaos/reset").json()
    assert out["chaos_files_removed"] == 1
    assert (real / "data.txt").exists() and (real / "application.log").exists()


def test_other_log_files_count_toward_the_ratio(real):
    (real / "application.log").write_bytes(b"x" * 1024 * 1024)         # 1 MiB of 4
    assert scraped_ratio() == pytest.approx(0.30 + 0.25, abs=0.001)
    client.post("/chaos/disk", json={"ratio": 0.95})
    assert scraped_ratio() == pytest.approx(0.95, abs=0.01)


def test_the_metric_follows_the_disk_between_scrapes(real):
    before = scraped_ratio()
    (real / "later.log").write_bytes(b"x" * 1024 * 1024)
    assert scraped_ratio() > before


@pytest.mark.parametrize("ratio", [-0.1, 1.0, 1.5])
def test_a_ratio_that_would_fill_the_disk_is_refused(real, ratio):
    assert client.post("/chaos/disk", json={"ratio": ratio}).status_code == 422
    assert not list(real.glob("*.log"))


def test_chaos_stays_off_unless_enabled(real, monkeypatch):
    monkeypatch.delenv("CHAOS_ENABLED")
    assert client.post("/chaos/disk", json={"ratio": 0.95}).status_code == 404
    assert client.post("/chaos/reset").status_code == 404
    assert not list(real.glob("*.log"))


def test_without_a_log_directory_it_falls_back_to_a_stored_value(monkeypatch, tmp_path):
    monkeypatch.setenv("CHAOS_ENABLED", "true")
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "does-not-exist"))
    r = client.post("/chaos/disk", json={"ratio": 0.95}).json()
    assert r["mode"] == "simulated" and r["ratio"] == 0.95 and scraped_ratio() == 0.95
    assert client.get("/disk").json()["mode"] == "simulated"
    client.post("/chaos/reset")
    assert scraped_ratio() == 0.30


def test_the_manifest_mounts_a_bounded_volume_where_the_playbook_deletes():
    docs = list(yaml.safe_load_all((ROOT / "k8s" / "database.yaml").read_text()))
    deploy = next(d for d in docs if d and d["kind"] == "Deployment")
    spec = deploy["spec"]["template"]["spec"]
    container = spec["containers"][0]
    env = {e["name"]: e["value"] for e in container["env"]}
    assert env["LOG_DIR"] == "/var/log/app" and "CHAOS_ENABLED" not in env
    assert {"name": "app-logs", "mountPath": "/var/log/app"} in container["volumeMounts"]
    volume = next(v for v in spec["volumes"] if v["name"] == "app-logs")
    assert volume["emptyDir"]["sizeLimit"] == "100Mi" and float(env["DB_DISK_CAPACITY_MB"]) < 100
    playbook = yaml.safe_load((ROOT / "ansible" / "playbooks" / "clear_logs.yml").read_text())[0]
    assert playbook["vars"]["approved_log_dir"] == env["LOG_DIR"] and playbook["vars"]["log_dir"] == env["LOG_DIR"]
