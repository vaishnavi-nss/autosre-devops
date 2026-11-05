"""The AutoSRE service dependency chain (brief section 3a).

Requests flow downward:

    frontend -> api-gateway -> transaction-service -> database

Failures cascade the other way. A database problem shows up as symptoms in
every service above it, which is the whole basis of the root-cause ranking in
brief section 5 step 3: the root cause is the *deepest* alerting service.

CHAIN is ordered deepest-first, so a lower index means "closer to the
database" and therefore more likely to be the root cause.
"""

from __future__ import annotations

CHAIN: list[str] = [
    "database",
    "transaction-service",
    "api-gateway",
    "frontend",
]

#: Who each service calls. Useful when the chain grows into a real graph.
CALLS: dict[str, str | None] = {
    "frontend": "api-gateway",
    "api-gateway": "transaction-service",
    "transaction-service": "database",
    "database": None,
}


def depth(service: str) -> int | None:
    """Return the service's position in the chain, or None if it is unknown.

    0 is the database (deepest), 3 is the frontend (shallowest).
    """
    try:
        return CHAIN.index(service)
    except ValueError:
        return None


def is_known(service: str) -> bool:
    """True if the service is part of the modelled chain."""
    return service in CHAIN
