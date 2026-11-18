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

def _load_scenarios() -> dict:
    """The scenario table lives in the runner, which is the only thing that injects faults; the console just offers what the runner knows."""
    import importlib.util
    path = Path(__file__).resolve().parents[1] / "scenarios" / "run_scenarios.py"
    spec = importlib.util.spec_from_file_location("run_scenarios_for_console", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["run_scenarios_for_console"] = mod
    spec.loader.exec_module(mod)
    return mod.SCENARIOS


SCENARIOS = _load_scenarios()
FAMILIES = {
    "pool": "Database pool exhaustion", "memory": "High memory", "disk": "Database disk full", "service_down": "A service is down",
    "bad_rollout": "A bad rollout", "selector": "A Service selects nothing", "unknown": "Faults with no known cause (must be handed to a person)",
}


def families() -> list[dict]:
    """The cards of the page: one per family, with its variations in the order the runner lists them."""
    out: dict[str, dict] = {}
    for key, spec in SCENARIOS.items():
        fam = out.setdefault(spec["family"], {"family": spec["family"], "title": FAMILIES.get(spec["family"], spec["family"]), "variants": []})
        fam["variants"].append({"key": key, "label": spec["label"], "kind": spec["kind"], "expect": spec["expect"], "playbook": spec.get("playbook")})
    return list(out.values())


MODES = ("simulate", "live")
COOLDOWN_S = 60
# The two switches of the correlation engine that the team may change on purpose. Both default to the safe value.
ENGINE_SWITCHES = {
    "second_opinion": {"env": "SECOND_OPINION_MODE", "values": {"shadow": "shadow", "gate": "gate"}, "default": "shadow"},
    "ambiguity": {"env": "ESCALATE_ON_AMBIGUITY", "values": {"off": None, "on": "true"}, "default": "off"},     # None = unset the variable
}
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


def validate_engine_change(body: dict) -> tuple[str | None, str | None, str | None]:
    """(setting, value, error)."""
    setting, value = body.get("setting"), body.get("value")
    if setting not in ENGINE_SWITCHES:
        return None, None, f"unknown setting {setting!r}; choose one of {', '.join(ENGINE_SWITCHES)}"
    if value not in ENGINE_SWITCHES[setting]["values"]:
        return None, None, f"unknown value {value!r} for {setting}; choose one of {', '.join(ENGINE_SWITCHES[setting]['values'])}"
    return setting, value, None


def engine_env_arg(setting: str, value: str) -> str:
    """The `kubectl set env` argument: NAME=value, or NAME- to remove the variable (which is how a switch goes back to off)."""
    env, target = ENGINE_SWITCHES[setting]["env"], ENGINE_SWITCHES[setting]["values"][value]
    return f"{env}={target}" if target is not None else f"{env}-"


def parse_engine_env(raw: str) -> dict:
    """Read the two switches from the engine Deployment's env list (JSON). Missing means the default."""
    try:
        env = {e.get("name"): e.get("value") for e in json.loads(raw or "[]")}
    except (ValueError, TypeError, AttributeError):
        return {}
    mode = env.get("SECOND_OPINION_MODE") or "shadow"
    amb = str(env.get("ESCALATE_ON_AMBIGUITY") or "").lower() in ("1", "true", "yes")
    return {"second_opinion": mode if mode in ("shadow", "gate") else "unknown", "ambiguity": "on" if amb else "off"}


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
        self._engine = {"second_opinion": "starting", "ambiguity": "starting"}

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

    def engine_switches(self) -> dict:
        return dict(self._engine)

    def refresh_engine(self) -> None:
        try:
            raw = subprocess.run(["kubectl", "-n", "autosre", "get", "deploy", "correlation-engine", "-o", "jsonpath={.spec.template.spec.containers[0].env}"],
                                 capture_output=True, text=True, timeout=10).stdout.strip()
            parsed = parse_engine_env(raw)
            if parsed:
                self._engine = parsed
        except Exception:                                       # noqa: BLE001 - keep the last value
            if self._engine["second_opinion"] == "starting":
                self._engine = {"second_opinion": "unknown", "ambiguity": "unknown"}

    def set_engine(self, setting: str, value: str) -> tuple[bool, str]:
        """Change one engine switch. Never while a scenario run is in progress (the runner restores the safe defaults itself and the two would race)."""
        with self.lock:
            if self.running():
                return False, "a scenario run is in progress; change the switches when it has finished"
            try:
                done = subprocess.run(["kubectl", "-n", "autosre", "set", "env", "deploy/correlation-engine", engine_env_arg(setting, value)],
                                      capture_output=True, text=True, timeout=30)
            except Exception as exc:                            # noqa: BLE001
                return False, f"could not change the switch: {type(exc).__name__}"
            if done.returncode != 0:
                return False, "kubectl refused the change: " + (done.stderr or "").strip()[:200]
            self._engine = {**self._engine, setting: value}
            self.record_change(setting, value)
            return True, f"{setting} is now {value}. The engine restarts (a few seconds); its history is reloaded from its volume."

    def record_change(self, setting: str, value: str) -> None:
        """Add the change to the engine's event log (shown on the website's Settings page and copied to the S3 bucket). Best effort."""
        body = json.dumps({"kind": "change", "setting": {"second_opinion": "second_opinion_mode", "ambiguity": "ambiguity_rule"}[setting], "value": value,
                           "by": "fault console", "reason": "changed from the fault console; the runner restores the defaults after its next run"})
        try:
            subprocess.run(["kubectl", "create", "--raw", "/api/v1/namespaces/autosre/services/correlation-engine:8000/proxy/events", "-f", "-"],
                           input=body, capture_output=True, text=True, timeout=20)
        except Exception:                                       # noqa: BLE001 - the switch itself already worked
            pass

    def start_refresher(self, every_s: float = 5.0) -> threading.Thread:
        def loop():
            while True:
                self.refresh_auto()
                self.refresh_engine()
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
        return {"running": running, "job": self.job, "auto_remediate": self.auto_remediate(), "engine": self.engine_switches(), "allow_live": self.allow_live,
                "scenarios": {k: v["kind"] for k, v in SCENARIOS.items()}, "families": families(), "log": lines[-60:], **summarize_log(lines)}


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
            if self.path == "/api/engine":
                setting, value, err = validate_engine_change(body)
                if err:
                    return self._json(400, {"error": err})
                ok, msg = console.set_engine(setting, value)
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
