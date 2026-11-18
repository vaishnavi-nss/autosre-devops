#!/usr/bin/env python3
"""Keep AutoSRE's history in the S3 bucket, because when the lab restarts only the bucket survives.

What is kept (all append-only JSON lines, merged by id so two copies never lose a record):
  correlation-engine  /data/incidents.jsonl   every incident the engine decided, with its stage data
  correlation-engine  /data/events.jsonl      setting changes, scenario runs, acknowledgements and resolutions
  remediation-dispatcher /data/executions.jsonl  every playbook run with its timestamped steps
  ~/results/console-*  the fault console's run logs and the runner's JSON results

    python3 history_sync.py push     # cluster -> bucket   (run every couple of minutes by a timer, and after each run)
    python3 history_sync.py pull     # bucket -> cluster   (run by bring-up.sh after a lab restart; then the engine and dispatcher are restarted)

Standard library only; it shells out to kubectl and the aws CLI, which the lab instance has. It never deletes anything.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

NS = "autosre"
FILES = (("correlation-engine", "/data/incidents.jsonl", "incidents.jsonl"),
         ("correlation-engine", "/data/events.jsonl", "events.jsonl"),
         ("remediation-dispatcher", "/data/executions.jsonl", "executions.jsonl"))
SORT_FIELDS = ("received_at", "started_at", "t")


def _order(rec: dict, pos: int) -> tuple:
    m = re.search(r"(\d+)$", str(rec.get("id", "")))
    when = next((str(rec[f]) for f in SORT_FIELDS if rec.get(f)), "")
    return (when, int(m.group(1)) if m else 0, pos)


def merge_jsonl(*texts: str) -> str:
    """Union of the lines of every text, de-duplicated by `id` (or by the whole line when a record has none), oldest first.
    Lines that are not valid JSON are dropped: a half-written last line must never poison the bucket copy."""
    seen: dict[str, dict] = {}
    pos = 0
    for text in texts:
        for line in (text or "").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict):
                continue
            key = str(rec["id"]) if rec.get("id") else line
            if key not in seen:
                seen[key] = rec
                rec["__pos"] = pos
                pos += 1
    ordered = sorted(seen.values(), key=lambda r: _order(r, r["__pos"]))
    return "".join(json.dumps({k: v for k, v in r.items() if k != "__pos"}, separators=(",", ":")) + "\n" for r in ordered)


def count(text: str) -> int:
    return sum(1 for line in (text or "").splitlines() if line.strip())


class Sync:
    """`run(cmd, stdin=None) -> (returncode, stdout)` is injected so the logic is testable without a cluster or AWS."""

    def __init__(self, bucket: str, prefix: str, run, results_dir: Path | None = None, log=print):
        self.bucket, self.prefix, self.run, self.results_dir, self.log = bucket, prefix.strip("/"), run, results_dir, log

    def uri(self, name: str) -> str:
        return f"s3://{self.bucket}/{self.prefix}/history/{name}"

    # -- the two ends
    def pod_read(self, deploy: str, path: str) -> str | None:
        rc, out = self.run(["kubectl", "-n", NS, "exec", f"deploy/{deploy}", "--", "sh", "-c", f"cat {path} 2>/dev/null; true"])
        return out if rc == 0 else None

    def pod_write(self, deploy: str, path: str, text: str) -> bool:
        rc, _ = self.run(["kubectl", "-n", NS, "exec", "-i", f"deploy/{deploy}", "--", "sh", "-c", f"cat > {path}.new && mv {path}.new {path}"], stdin=text)
        return rc == 0

    def s3_read(self, name: str) -> str:
        rc, out = self.run(["aws", "s3", "cp", self.uri(name), "-", "--only-show-errors"])
        return out if rc == 0 else ""                       # not there yet is normal on the first push

    def s3_write(self, name: str, text: str) -> bool:
        rc, _ = self.run(["aws", "s3", "cp", "-", self.uri(name), "--only-show-errors"], stdin=text)
        return rc == 0

    # -- operations
    def push(self) -> dict:
        report = {}
        for deploy, path, name in FILES:
            local = self.pod_read(deploy, path)
            if local is None:
                self.log(f"push {name}: {deploy} not reachable, skipped")
                report[name] = "unreachable"
                continue
            remote = self.s3_read(name)
            merged = merge_jsonl(remote, local)
            if merged == merge_jsonl(remote):
                report[name] = f"unchanged ({count(merged)})"
            else:
                report[name] = f"uploaded {count(merged)} (bucket had {count(remote)}, cluster had {count(local)})" if self.s3_write(name, merged) else "UPLOAD FAILED"
            self.log(f"push {name}: {report[name]}")
        if self.results_dir and self.results_dir.exists():
            rc, _ = self.run(["aws", "s3", "sync", str(self.results_dir), f"s3://{self.bucket}/{self.prefix}/results", "--only-show-errors"])
            report["results"] = "synced" if rc == 0 else "SYNC FAILED"
            self.log(f"push results: {report['results']}")
        return report

    def pull(self) -> dict:
        report, changed = {}, False
        for deploy, path, name in FILES:
            remote = self.s3_read(name)
            if not remote.strip():
                report[name] = "nothing in the bucket"
                self.log(f"pull {name}: {report[name]}")
                continue
            local = self.pod_read(deploy, path)
            if local is None:
                self.log(f"pull {name}: {deploy} not reachable, skipped")
                report[name] = "unreachable"
                continue
            merged = merge_jsonl(local, remote)
            if merged == merge_jsonl(local):
                report[name] = f"already up to date ({count(merged)})"
            elif self.pod_write(deploy, path, merged):
                report[name] = f"restored {count(merged) - count(merge_jsonl(local))} record(s), now {count(merged)}"
                changed = True
            else:
                report[name] = "WRITE FAILED"
            self.log(f"pull {name}: {report[name]}")
        if self.results_dir:
            self.results_dir.mkdir(parents=True, exist_ok=True)
            rc, _ = self.run(["aws", "s3", "sync", f"s3://{self.bucket}/{self.prefix}/results", str(self.results_dir), "--only-show-errors"])
            report["results"] = "restored" if rc == 0 else "SYNC FAILED"
            self.log(f"pull results: {report['results']}")
        report["restart_needed"] = changed
        return report


def shell(cmd: list[str], stdin: str | None = None) -> tuple[int, str]:
    done = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=120)
    return done.returncode, done.stdout


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=("push", "pull"))
    ap.add_argument("--bucket", default=os.environ.get("BUCKET", "autosre-validation-841736140404"))
    ap.add_argument("--prefix", default="autosre")
    ap.add_argument("--results", default=str(Path.home() / "results"))
    a = ap.parse_args(argv)
    sync = Sync(a.bucket, a.prefix, shell, Path(a.results))
    report = getattr(sync, a.action)()
    print(json.dumps(report))
    failed = [k for k, v in report.items() if isinstance(v, str) and ("FAILED" in v)]
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
