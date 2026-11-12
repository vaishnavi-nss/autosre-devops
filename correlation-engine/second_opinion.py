"""Client for the second-opinion service, kept dependency-free so the engine image stays small.

The engine asks "do the raw metrics back this diagnosis?" after the rules have decided. The
call must never make an incident worse, so every failure - service down, slow, bad reply -
comes back as the verdict "unavailable" and the caller carries on as if it had not asked.
"""

from __future__ import annotations

import json
import re
import urllib.request
from datetime import datetime, timezone


# `for:` of each alert in monitoring/alert-rules.yaml (a test keeps this in step with the file). An alert's
# `startsAt` is when it began firing, which is this long, plus a scrape and an evaluation, after the fault.
FOR_SECONDS = {
    "APILatencyHigh": 60, "ErrorRateHigh": 60, "DBPoolNearExhaustion": 30,
    "DBDiskNearFull": 60, "PodRestartsHigh": 0, "ContainerMemoryHigh": 60,
}
SCRAPE_AND_EVAL_S = 15
DEFAULT_FOR_S = 60


def _starts_at(alert: dict) -> float | None:
    raw = alert.get("startsAt")
    if not raw:
        return None
    # Alertmanager writes nanoseconds; datetime only takes microseconds.
    raw = re.sub(r"(\.\d{6})\d+", r"\1", raw).replace("Z", "+00:00")
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if moment.year <= 1:                          # "0001-01-01..." means unset
        return None
    return moment.astimezone(timezone.utc).timestamp()


def earliest_start(alerts: list) -> float | None:
    """Epoch seconds of the earliest `startsAt` among the alerts, or None if none parse."""
    starts = [t for t in map(_starts_at, alerts) if t is not None]
    return min(starts) if starts else None


def estimate_fault_start(alerts: list, deciding: list | None = None) -> float | None:
    """When the fault most likely began, from the alerts that decided the diagnosis.

    Taking the earliest alert of the whole group went wrong live: an unrelated alert still firing from
    an earlier incident put the start minutes before the fault, and the scorer then compared the wrong
    window. So only the deciding symptoms count (all alerts if none of them is present), and each is
    wound back by its own `for:` delay plus a scrape and an evaluation.
    """
    pool = [a for a in alerts if a.get("labels", {}).get("alertname") in (deciding or ())] or list(alerts)
    estimates = []
    for alert in pool:
        started = _starts_at(alert)
        if started is not None:
            name = alert.get("labels", {}).get("alertname")
            estimates.append(started - FOR_SECONDS.get(name, DEFAULT_FOR_S) - SCRAPE_AND_EVAL_S)
    return min(estimates) if estimates else None


def ask(url: str, root_cause: str, service: str, incident_start: float, timeout: float = 4.0,
        fault_start: float | None = None) -> dict:
    """POST to {url}/opinion. Returns the service's verdict dict, or {"verdict": "unavailable"}."""
    payload = {"root_cause": root_cause, "service": service, "incident_start": incident_start}
    if fault_start is not None:
        payload["fault_start"] = fault_start
    body = json.dumps(payload).encode()
    request = urllib.request.Request(f"{url.rstrip('/')}/opinion", data=body,
                                     headers={"content-type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            reply = json.load(response)
        if reply.get("verdict") not in ("agree", "disagree", "no_opinion"):
            return {"verdict": "unavailable", "reason": "unrecognised reply"}
        return reply
    except Exception as exc:                     # noqa: BLE001 - see module docstring
        return {"verdict": "unavailable", "reason": f"{type(exc).__name__}: {exc}"}
