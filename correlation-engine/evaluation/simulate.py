"""Synthetic labelled incidents for evaluating the correlation engine.

IMPORTANT - READ BEFORE QUOTING ANY NUMBER FROM THIS
    This is a SIMULATION. It encodes assumptions about how each fault shows up
    as alerts, and the person who wrote it also wrote the signatures. So an
    accuracy figure measured on simulated data answers "does the engine stay
    correct when symptoms drop out, cascade, and get buried in noise?" - it does
    NOT answer "how accurate is the engine on real production incidents?".
    Only incidents captured from the live cluster (see CAPTURE_PATH in app.py and
    `evaluate.py --replay`) can answer that. Report the two separately.

The kinds of incident
    one per known cause   the fault is real and the engine should name it
    unknown               a fault no signature describes (a bad deploy, a network
                          blip): only consequence-type alerts. Expected: escalate.
    deep_fault            a fault in a DEEPER service that no signature describes
                          (the database degrades) which in turn exhausts the
                          transaction service's connection pool. The pool alerts
                          are real but they are a symptom, so restarting the
                          transaction service would be the wrong fix.
                          Expected: escalate.

How an incident is built
    1. The deciding symptom fires on the owning service, unless it drops out.
    2. Each consequence fires on its usual service, unless it drops out.
    3. ErrorRateHigh cascades upstream: when the transaction service fails, the
       gateway and the frontend (which call it) start returning errors too.
    4. Noise: unrelated alerts on random services, and - rarely - a deciding
       symptom belonging to a DIFFERENT cause (a confounder).

The label is the cause that was injected, i.e. ground truth by construction.
`None` means "no known cause" - the engine is expected to escalate. Every record
also carries `meta` describing what was done to it, so failures can be attributed.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from dependency_graph import CHAIN, depth
from signatures import SIGNATURES

SERVICES = list(CHAIN)

#: Where each consequence of each cause normally shows up.
PLACEMENT: dict[str, dict[str, str]] = {
    "db_connection_pool_exhaustion": {
        "ErrorRateHigh": "transaction-service",
        "PodRestartsHigh": "transaction-service",
        "APILatencyHigh": "api-gateway",
    },
    "db_disk_full": {
        "ErrorRateHigh": "transaction-service",
        "APILatencyHigh": "api-gateway",
    },
    "high_memory_usage": {
        "PodRestartsHigh": "transaction-service",
        "APILatencyHigh": "api-gateway",
    },
}

#: Consequence-type alerts an unrelated fault could produce. None of them is a
#: deciding symptom of any known cause.
UNKNOWN_CATALOGUE: list[tuple[str, str]] = (
    [("ErrorRateHigh", s) for s in SERVICES]
    + [("PodRestartsHigh", s) for s in SERVICES]
    + [("APILatencyHigh", "api-gateway")]
)

#: What a degraded database looks like when no signature describes the fault.
DEEP_FAULT_SIGNS: list[tuple[str, str]] = [
    ("ErrorRateHigh", "database"),
    ("PodRestartsHigh", "database"),
]

#: Background noise: anything the rules can emit that is not a deciding symptom
#: of a known cause. ContainerMemoryHigh is only a deciding symptom on the
#: transaction service, so on the other services it is just noise.
NOISE_CATALOGUE: list[tuple[str, str]] = UNKNOWN_CATALOGUE + [
    ("ContainerMemoryHigh", s) for s in SERVICES if s != "transaction-service"
]

SEVERITY = {
    "DBPoolNearExhaustion": "critical",
    "DBDiskNearFull": "critical",
}

#: Share of incidents of each kind. Kinds that are not causes have label None.
#: These proportions are ASSUMPTIONS, not measurements.
KIND_MIX: list[tuple[str, float]] = [
    ("db_connection_pool_exhaustion", 0.25),
    ("db_disk_full", 0.25),
    ("high_memory_usage", 0.24),
    ("unknown", 0.18),
    ("deep_fault", 0.08),
]


@dataclass(frozen=True)
class Noise:
    """How messy the incidents are. Every field is a probability."""

    p_deciding: float    # the deciding symptom fires and is observed
    p_support: float     # each consequence fires
    p_cascade: float     # an error cascades to each upstream service
    p_spurious: float    # the incident also contains unrelated alerts
    p_confounder: float  # it also contains another cause's deciding symptom
    max_spurious: int = 3


PRESETS: dict[str, Noise] = {
    # Every symptom present, nothing else. Sanity check only.
    "clean": Noise(1.00, 1.00, 1.00, 0.00, 0.00),
    # A reasonable guess at a working cluster.
    "moderate": Noise(0.95, 0.80, 0.70, 0.25, 0.05),
    # Flapping alerts, lots of unrelated noise, frequent confounders.
    "hard": Noise(0.85, 0.60, 0.50, 0.50, 0.15),
}


def label_for(kind: str) -> str | None:
    """The ground-truth label: the cause for a known cause, else None."""
    return kind if kind in SIGNATURES else None


def _upstream(service: str) -> list[str]:
    """Services that call `service`, directly or indirectly."""
    return [s for s in SERVICES if depth(s) > depth(service)]


def _add_cause_symptoms(
    rng: random.Random, cause: str, noise: Noise, pairs: list, meta: dict
) -> None:
    """Add the deciding symptom and consequences that `cause` produces."""
    spec = SIGNATURES[cause]
    if rng.random() < noise.p_deciding:
        pairs.append((spec["required_symptoms"][0], spec["owner"]))
    else:
        meta["deciding_dropped"] = True
    for symptom, service in PLACEMENT[cause].items():
        if rng.random() >= noise.p_support:
            continue
        pairs.append((symptom, service))
        if symptom == "ErrorRateHigh":
            for up in _upstream(service):
                if rng.random() < noise.p_cascade:
                    pairs.append((symptom, up))


def make_incident(
    rng: random.Random, kind: str, noise: Noise
) -> tuple[list[tuple[str, str]], dict]:
    """One incident as (ordered (alertname, service) pairs, metadata)."""
    pairs: list[tuple[str, str]] = []
    meta: dict = {"kind": kind, "deciding_dropped": False, "confounder": None, "spurious": []}

    if kind == "unknown":
        for _ in range(rng.randint(1, 3)):
            pairs.append(rng.choice(UNKNOWN_CATALOGUE))

    elif kind == "deep_fault":
        # The real fault is in the database and nothing describes it...
        pairs.append(rng.choice(DEEP_FAULT_SIGNS))
        # ...but it starves the transaction service's pool, which then alerts.
        _add_cause_symptoms(rng, "db_connection_pool_exhaustion", noise, pairs, meta)

    else:
        _add_cause_symptoms(rng, kind, noise, pairs, meta)
        if rng.random() < noise.p_confounder:
            other = rng.choice([c for c in SIGNATURES if c != kind])
            other_spec = SIGNATURES[other]
            pairs.append((other_spec["required_symptoms"][0], other_spec["owner"]))
            meta["confounder"] = other

    if rng.random() < noise.p_spurious:
        for _ in range(rng.randint(1, noise.max_spurious)):
            noise_pair = rng.choice(NOISE_CATALOGUE)
            pairs.append(noise_pair)
            meta["spurious"].append(list(noise_pair))

    seen: set[tuple[str, str]] = set()
    unique = [p for p in pairs if not (p in seen or seen.add(p))]
    rng.shuffle(unique)
    return unique, meta


def to_payload(pairs: list[tuple[str, str]], index: int) -> dict:
    """Wrap (alertname, service) pairs as an Alertmanager v4 webhook payload."""
    return {
        "version": "4",
        "groupKey": f"sim-{index}",
        "status": "firing",
        "receiver": "autosre-correlation",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": name,
                    "service": service,
                    "namespace": "autosre",
                    "severity": SEVERITY.get(name, "warning"),
                },
                "annotations": {},
            }
            for name, service in pairs
        ],
    }


def generate(n: int, noise: Noise, seed: int) -> list[dict]:
    """`n` labelled incidents: [{"label", "payload", "meta"}, ...].

    Deterministic for a given (n, noise, seed). An incident in which every
    symptom dropped out would never reach the engine - Alertmanager only sends
    groups that contain at least one alert - so it is redrawn.
    """
    rng = random.Random(seed)
    kinds = [k for k, _ in KIND_MIX]
    weights = [w for _, w in KIND_MIX]

    dataset: list[dict] = []
    while len(dataset) < n:
        kind = rng.choices(kinds, weights)[0]
        pairs, meta = make_incident(rng, kind, noise)
        if not pairs:
            continue
        dataset.append(
            {"label": label_for(kind), "payload": to_payload(pairs, len(dataset)), "meta": meta}
        )
    return dataset


def _check_consistency() -> None:
    """Fail loudly if the signatures change and this simulator is left behind."""
    for cause, spec in SIGNATURES.items():
        consequences = set(spec["weights"]) - set(spec["required_symptoms"])
        placed = set(PLACEMENT.get(cause, {}))
        if consequences != placed:
            raise AssertionError(
                f"simulate.PLACEMENT is out of sync with signatures.py for {cause}: "
                f"signature consequences {sorted(consequences)} vs placed {sorted(placed)}"
            )
    assert abs(sum(w for _, w in KIND_MIX) - 1.0) < 1e-9, "KIND_MIX must sum to 1"


_check_consistency()
