#!/usr/bin/env python3
"""AutoSRE fault console: a small web page with buttons that start one real fault scenario on the lab cluster.

It runs on the lab instance (as the user that owns kubectl) and does nothing itself except start and stop
`scripts/scenarios/run_scenarios.py`, so every safety rule of that runner applies unchanged:
  * isolated `autosre` namespace only, one scenario at a time, preflight must pass or the run stops;
  * SIMULATE  = the real fault is injected but AUTO_REMEDIATE stays false: the pipeline records decisions and nothing is fixed;
  * LIVE      = AUTO_REMEDIATE is switched on for that run only and the runner always restores it to false afterwards.
LIVE is refused unless the server was started with FAULT_CONSOLE_ALLOW_LIVE=1 AND the request carries the typed word LIVE.
Every action needs the access token (a random value stored in the token file, readable only by its owner).

    FAULT_CONSOLE_ALLOW_LIVE=1 python3 scripts/fault_console/console.py --port 8082

Standard library only.
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SCENARIOS = {
    "pool": "Database pool exhaustion: every slot of the service's bounded pool is leaked, so requests really time out",
    "memory": "High memory: the container holds real memory and every request does work proportional to it",
    "disk": "Database disk full: real bytes fill the database's bounded log directory until writes fail (HTTP 507)",
}
MODES = ("simulate", "live")
COOLDOWN_S = 60
RUNNER_PATTERN = "run_scenario[s]"          # the [s] keeps pgrep from matching itself


# ------------------------------------------------------------------------------------------------ pure helpers (unit tested)
def validate_request(body: dict, allow_live: bool) -> tuple[str | None, str | None, str | None]:
    """(scenario, mode, error). Exactly one error, or none."""
    scenario, mode = body.get("scenario"), body.get("mode", "simulate")
    if scenario not in SCENARIOS:
        return None, None, f"unknown scenario {scenario!r}; choose one of {', '.join(SCENARIOS)}"
    if mode not in MODES:
        return None, None, f"unknown mode {mode!r}; choose simulate or live"
    if mode == "live":
        if not allow_live:
            return None, None, "live runs are switched off on this server (start it with FAULT_CONSOLE_ALLOW_LIVE=1)"
        if body.get("confirm") != "LIVE":
            return None, None, "a live run needs the confirmation word LIVE"
    return scenario, mode, None


def build_command(repo: Path, out: Path, scenario: str, mode: str) -> list[str]:
    cmd = [sys.executable, str(repo / "scripts" / "scenarios" / "run_scenarios.py"), "--out", str(out),
           "--scenarios", scenario, "--cooldown", str(COOLDOWN_S)]
    if mode == "live":
        cmd.append("--live")
    return cmd


def summarize_log(lines: list[str]) -> dict:
    """What the runner has done so far, from its own log lines. Only the runner's words are used, nothing is inferred."""
    phase, outcome = "starting", None
    for line in lines:
        text = line.split("] ", 1)[-1].strip()
        if text.startswith("preflight"):
            phase = "preflight"
        elif text.startswith("enabling fault injection"):
            phase = "preparing"
        elif text.startswith("LIVE:"):
            phase = "live remediation on"
        elif text.startswith("==="):
            phase = "fault injected"
        elif text.startswith("->"):
            outcome = text[2:].strip()
        elif text.startswith("restoring"):
            phase = "restoring safe settings"
        elif text.startswith("STOPPED"):
            phase, outcome = "stopped", text
        elif text.startswith("done:"):
            phase = "finished"
    return {"phase": phase, "outcome": outcome}


def token_ok(supplied: str | None, expected: str) -> bool:
    return bool(supplied) and hmac.compare_digest(supplied.encode(), expected.encode())


# ------------------------------------------------------------------------------------------------ server state
class Console:
    def __init__(self, repo: Path, results: Path, token: str, allow_live: bool):
        self.repo, self.results, self.token, self.allow_live = repo, results, token, allow_live
        self.lock = threading.Lock()
        self.proc: subprocess.Popen | None = None
        self.job: dict | None = None
        self._auto = "starting"

    def runner_pids(self) -> list[str]:
        out = subprocess.run(["pgrep", "-f", RUNNER_PATTERN], capture_output=True, text=True).stdout.split()
        return out

    def auto_remediate(self) -> str:
        """The last value the background refresher read. It never calls kubectl itself, so a slow or busy cluster cannot make the status
        page hang (it did once, when every poll ran kubectl on the request thread)."""
        return self._auto

    def refresh_auto(self) -> None:
        try:
            raw = subprocess.run(["kubectl", "-n", "autosre", "get", "deploy", "remediation-dispatcher", "-o",
                                  "jsonpath={.spec.template.spec.containers[0].env[?(@.name=='AUTO_REMEDIATE')].value}"],
                                 capture_output=True, text=True, timeout=10).stdout.strip()
            self._auto = raw or "unknown"
        except Exception:                                       # noqa: BLE001 - keep the last value, mark it unknown only if never read
            if self._auto == "starting":
                self._auto = "unknown"

    def start_refresher(self, every_s: float = 5.0) -> threading.Thread:
        def loop():
            while True:
                self.refresh_auto()
                time.sleep(every_s)
        thread = threading.Thread(target=loop, name="auto-remediate-refresher", daemon=True)
        thread.start()
        return thread

    def running(self) -> bool:
        return bool(self.proc and self.proc.poll() is None) or bool(self.runner_pids())

    def start(self, scenario: str, mode: str) -> tuple[bool, str]:
        with self.lock:
            if self.running():
                return False, "a scenario run is already in progress; wait for it to finish (or stop it) first"
            stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
            out = self.results / f"console-{stamp}-{scenario}-{mode}"
            out.mkdir(parents=True, exist_ok=True)
            logf = out / "run.log"
            handle = open(logf, "w", encoding="utf-8")
            self.proc = subprocess.Popen(build_command(self.repo, out, scenario, mode), cwd=self.repo, stdout=handle,
                                         stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
            self.job = {"scenario": scenario, "mode": mode, "started": time.strftime("%H:%M:%S", time.gmtime()), "log": str(logf), "out": str(out)}
            return True, f"{mode} run of {scenario} started"

    def stop(self) -> str:
        pids = self.runner_pids()
        for pid in pids:
            try:
                os.kill(int(pid), signal.SIGTERM)               # the runner restores the safe settings on SIGTERM
            except (ProcessLookupError, ValueError):
                pass
        return "stop requested: the runner is restoring the safe settings" if pids else "nothing is running"

    def status(self) -> dict:
        lines: list[str] = []
        if self.job:
            try:
                lines = Path(self.job["log"]).read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                lines = []
        running = self.running()
        return {"running": running, "job": self.job, "auto_remediate": self.auto_remediate(), "allow_live": self.allow_live,
                "scenarios": SCENARIOS, "log": lines[-60:], **summarize_log(lines)}


PAGE = (Path(__file__).with_name("page.html")).read_text(encoding="utf-8") if Path(__file__).with_name("page.html").exists() else "<h1>page.html missing</h1>"


def make_handler(console: Console):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):                           # keep the server quiet
            pass

        def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj: dict) -> None:
            self._send(code, json.dumps(obj).encode())

        def do_GET(self):                                       # noqa: N802
            if self.path == "/api/status":
                self._json(200, console.status())
            elif self.path in ("/", "/index.html"):
                self._send(200, PAGE.encode(), "text/html; charset=utf-8")
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self):                                      # noqa: N802
            if not token_ok(self.headers.get("X-Token"), console.token):
                return self._json(401, {"error": "missing or wrong access token"})
            try:
                length = min(int(self.headers.get("Content-Length") or 0), 4096)
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return self._json(400, {"error": "bad JSON"})
            if self.path == "/api/run":
                scenario, mode, err = validate_request(body, console.allow_live)
                if err:
                    return self._json(400, {"error": err})
                ok, msg = console.start(scenario, mode)
                return self._json(200 if ok else 409, {"ok": ok, "message": msg})
            if self.path == "/api/stop":
                return self._json(200, {"ok": True, "message": console.stop()})
            return self._json(404, {"error": "not found"})

    return Handler


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8082)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--repo", default=str(Path(__file__).resolve().parents[2]))
    ap.add_argument("--results", default=str(Path.home() / "results"))
    ap.add_argument("--token-file", default=str(Path.home() / ".fault-console-token"))
    a = ap.parse_args()
    tf = Path(a.token_file)
    if not tf.exists():
        tf.write_text(secrets.token_urlsafe(18) + "\n")
        tf.chmod(0o600)
    token = tf.read_text().strip()
    allow_live = os.environ.get("FAULT_CONSOLE_ALLOW_LIVE") == "1"
    Path(a.results).mkdir(parents=True, exist_ok=True)
    console = Console(Path(a.repo), Path(a.results), token, allow_live)
    console.start_refresher()
    print(f"fault console on {a.host}:{a.port} (live runs {'ALLOWED' if allow_live else 'off'}); token file {tf}", flush=True)
    ThreadingHTTPServer((a.host, a.port), make_handler(console)).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
