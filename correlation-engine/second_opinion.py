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


def earliest_start(alerts: list) -> float | None:
    """Epoch seconds of the earliest `startsAt` among the alerts, or None if none parse."""
    starts = []
    for alert in alerts:
        raw = alert.get("startsAt")
        if not raw:
            continue
        # Alertmanager writes nanoseconds; datetime only takes microseconds.
        raw = re.sub(r"(\.\d{6})\d+", r"\1", raw).replace("Z", "+00:00")
        try:
            moment = datetime.fromisoformat(raw)
        except ValueError:
            continue
        if moment.year > 1:                      # "0001-01-01..." means unset
            starts.append(moment.astimezone(timezone.utc).timestamp())
    return min(starts) if starts else None


def ask(url: str, root_cause: str, service: str, incident_start: float, timeout: float = 4.0) -> dict:
    """POST to {url}/opinion. Returns the service's verdict dict, or {"verdict": "unavailable"}."""
    body = json.dumps({"root_cause": root_cause, "service": service,
                       "incident_start": incident_start}).encode()
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
