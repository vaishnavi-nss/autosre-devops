"""SQLite reservations survive dispatcher restarts on its PVC."""
import json
import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path


class RemediationState:
    def __init__(self, path, cooldown_seconds=600):
        self.path, self.cooldown_seconds = Path(path), cooldown_seconds

    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("""CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY, root_cause TEXT NOT NULL, service TEXT NOT NULL,
            requested_at REAL NOT NULL, status TEXT NOT NULL, details TEXT NOT NULL,
            confidence REAL, playbook TEXT)""")
        return connection

    @staticmethod
    def decode(row):
        if row is None:
            return None
        result = dict(row)
        result["details"] = json.loads(result["details"])
        # Historical jobs did not record confidence; do not infer it from thresholds.
        result.setdefault("confidence", None)
        result["playbook"] = result.get("playbook") or result["details"].get("playbook")
        return result

    def reserve(self, root_cause, service, confidence=None, playbook=None):
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            # Migrate only on the write path, under its existing reservation lock.
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(jobs)")}
            for name, sql_type in (("confidence", "REAL"), ("playbook", "TEXT")):
                if name not in columns:
                    connection.execute(f"ALTER TABLE jobs ADD COLUMN {name} {sql_type}")
            previous = self.decode(connection.execute(
                "SELECT * FROM jobs WHERE root_cause=? AND service=? ORDER BY requested_at DESC LIMIT 1",
                (root_cause, service)).fetchone())
            now = time.time()
            if previous:
                if previous["status"] == "failed":
                    return "blocked", previous
                if now - previous["requested_at"] < self.cooldown_seconds:
                    return "cooldown", previous
                if previous["status"] in ("accepted", "running"):
                    return "blocked", previous
            job = {"id": str(uuid.uuid4()), "root_cause": root_cause, "service": service,
                   "requested_at": now, "status": "accepted", "details": {},
                   "confidence": confidence, "playbook": playbook}
            connection.execute(
                "INSERT INTO jobs (id, root_cause, service, requested_at, status, details, confidence, playbook) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (job["id"], root_cause, service, now, "accepted", "{}", confidence, playbook))
            return "accepted", job

    def finish(self, job_id, status, details):
        with self.connect() as connection:
            connection.execute("UPDATE jobs SET status=?, details=? WHERE id=?", (status, json.dumps(details), job_id))

    def get(self, job_id):
        with self.connect() as connection:
            return self.decode(connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def latest(self):
        """Read history without creating/migrating the database or reserving work."""
        if not self.path.exists():
            return None
        uri = self.path.resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=2)) as connection:
            connection.row_factory = sqlite3.Row
            if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='jobs'").fetchone():
                return None
            return self.decode(connection.execute(
                "SELECT * FROM jobs ORDER BY requested_at DESC, rowid DESC LIMIT 1").fetchone())
