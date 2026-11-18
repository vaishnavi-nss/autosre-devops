"""The engine's side of the second opinion: asking, annotating, and (in gate mode) vetoing.

The scorer service is faked; what matters here is that it can never make an incident worse.
"""

import app as engine
import second_opinion as so


def fake(verdict, reason="because"):
    calls = []

    def ask(url, cause, service, started, timeout, fault_start=None):
        calls.append((url, cause, service, started))
        ask.fault_starts.append(fault_start)
        return {"verdict": verdict, "reason": reason}

    ask.calls = calls
    ask.fault_starts = []
    return ask


def enable(monkeypatch, mode, ask):
    monkeypatch.setattr(engine, "SECOND_OPINION_URL", "http://scorer")
    monkeypatch.setattr(engine, "SECOND_OPINION_MODE", mode)
    monkeypatch.setattr(so, "ask", ask)


def test_earliest_start_parses_alertmanager_timestamps_including_nanoseconds():
    alerts = [{"startsAt": "2026-10-31T04:08:12.153456789Z"}, {"startsAt": "2026-10-31T04:07:00Z"},
              {"startsAt": "0001-01-01T00:00:00Z"}, {}]
    assert so.earliest_start(alerts) == 1793419620.0   # 2026-10-31T04:07:00Z
    assert so.earliest_start([{}]) is None


def test_ask_never_raises_when_the_scorer_is_unreachable():
    reply = so.ask("http://127.0.0.1:9", "db_disk_full", "database", 1.0, timeout=0.5)
    assert reply["verdict"] == "unavailable"


def test_off_by_default_nothing_is_asked_or_added(client, sample_payload, monkeypatch):
    monkeypatch.setattr(engine, "SECOND_OPINION_URL", None)
    body = client.post("/alerts", json=sample_payload).json()
    assert body["root_cause"] == "db_connection_pool_exhaustion"
    assert "second_opinion" not in body


def test_shadow_mode_annotates_but_never_changes_the_decision(client, sample_payload, monkeypatch):
    ask = fake("disagree", "metrics point at database")
    enable(monkeypatch, "shadow", ask)
    body = client.post("/alerts", json=sample_payload).json()
    assert body["status"] == "correlated"
    assert body["root_cause"] == "db_connection_pool_exhaustion"
    assert body["second_opinion"]["verdict"] == "disagree"
    assert ask.calls[0][1:3] == ("db_connection_pool_exhaustion", "transaction-service")


def test_gate_mode_withholds_the_action_when_the_metrics_disagree(client, sample_payload, monkeypatch):
    enable(monkeypatch, "gate", fake("disagree", "metrics point at database"))
    body = client.post("/alerts", json=sample_payload).json()
    assert body["status"] == "vetoed" and body["action"] == "escalate_to_human"
    assert body["root_cause"] is None
    assert body["rule_root_cause"] == "db_connection_pool_exhaustion"
    assert "metrics point at database" in body["reason"]


def test_gate_mode_still_acts_on_agreement_silence_or_failure(client, sample_payload, monkeypatch):
    for verdict in ("agree", "no_opinion", "unavailable"):
        enable(monkeypatch, "gate", fake(verdict))
        body = client.post("/alerts", json=sample_payload).json()
        assert body["status"] == "correlated", verdict
        assert body["root_cause"] == "db_connection_pool_exhaustion", verdict


def test_a_veto_is_counted_and_does_not_count_as_an_identified_cause(client, sample_payload, monkeypatch):
    from prometheus_client import REGISTRY

    def value(name, labels):
        return REGISTRY.get_sample_value(name, labels) or 0.0

    identified = {"service": "correlation-engine", "root_cause": "db_connection_pool_exhaustion"}
    before_veto = value("vetoed_incidents_total", {"service": "correlation-engine"})
    before_identified = value("root_causes_identified_total", identified)
    enable(monkeypatch, "gate", fake("disagree"))
    client.post("/alerts", json=sample_payload)
    assert value("vetoed_incidents_total", {"service": "correlation-engine"}) == before_veto + 1
    assert value("root_causes_identified_total", identified) == before_identified


def test_unmatched_incidents_never_ask(client, sample_payload, monkeypatch):
    ask = fake("agree")
    enable(monkeypatch, "gate", ask)
    sample_payload["alerts"] = [a for a in sample_payload["alerts"]
                                if a["labels"]["alertname"] != "DBPoolNearExhaustion"]
    assert client.post("/alerts", json=sample_payload).json()["status"] == "unmatched"
    assert ask.calls == []


def alert(name, starts_at):
    return {"labels": {"alertname": name}, "startsAt": starts_at}


def test_a_stale_alert_from_an_earlier_incident_does_not_move_the_fault_start():
    """Run 7: APILatencyHigh still firing from the previous trial put the start 12 minutes early."""
    stale = alert("APILatencyHigh", "2026-11-11T19:29:00Z")
    deciding = alert("DBPoolNearExhaustion", "2026-11-11T19:42:20Z")
    start = so.estimate_fault_start([stale, deciding], ["DBPoolNearExhaustion"])
    # 19:42:20 minus its 30 s `for:` and a 15 s scrape and evaluation
    assert start == so._starts_at(deciding) - 30 - 15


def test_each_alert_is_wound_back_by_its_own_for_delay():
    a = alert("DBDiskNearFull", "2026-11-11T19:29:15Z")
    assert so.estimate_fault_start([a], ["DBDiskNearFull"]) == so._starts_at(a) - 60 - 15


def test_without_a_deciding_alert_all_alerts_are_used_and_none_gives_none():
    a = alert("ErrorRateHigh", "2026-11-11T19:29:15Z")
    assert so.estimate_fault_start([a], ["DBDiskNearFull"]) == so._starts_at(a) - 75
    assert so.estimate_fault_start([{}], ["DBDiskNearFull"]) is None


def test_engine_sends_the_estimated_fault_start(client, sample_payload, monkeypatch):
    ask = fake("agree")
    enable(monkeypatch, "shadow", ask)
    client.post("/alerts", json=sample_payload)
    assert ask.fault_starts[0] is not None
    assert ask.fault_starts[0] == so.estimate_fault_start(
        [a for a in sample_payload["alerts"] if a.get("status") == "firing"], ["DBPoolNearExhaustion"])


def test_for_seconds_match_the_alert_rules():
    import re
    from pathlib import Path
    text = (Path(__file__).resolve().parents[2] / "monitoring" / "alert-rules.yaml").read_text(encoding="utf-8")
    unit = {"s": 1, "m": 60}
    found = {}
    for block in re.split(r"-\s+alert:\s*", text)[1:]:
        name = block.split()[0]
        amount, kind = re.search(r"for:\s*(\d+)([sm])", block).groups()
        found[name] = int(amount) * unit[kind]
    assert found == so.FOR_SECONDS


# ---------------------------------------------------------------------------------------------------- transparency mode
from transparency import build_variants  # noqa: E402
from correlate import correlate  # noqa: E402


def alert(name, service):
    return {"status": "firing", "labels": {"alertname": name, "service": service, "severity": "critical"}, "startsAt": "2026-11-18T10:00:00Z"}


POOL = [alert("DBPoolNearExhaustion", "transaction-service"), alert("APILatencyHigh", "api-gateway")]


def rows(v):
    return {r["key"]: r for r in v["rows"]}


def test_variants_with_an_agreeing_opinion_all_act():
    v = build_variants(POOL, correlate(POOL, False), {"verdict": "agree"}, "shadow", False)
    r = rows(v)
    assert {k: x["action"] for k, x in r.items()} == {"rules": "act", "ml": "act", "amb": "act", "both": "act"}
    assert r["rules"]["active"] and not r["ml"]["active"] and not r["both"]["active"]          # shadow + ambiguity off is the rules-only row
    assert v["second_opinion_verdict"] == "agree"


def test_a_disagreeing_opinion_only_changes_the_rows_that_use_it():
    r = rows(build_variants(POOL, correlate(POOL, False), {"verdict": "disagree"}, "shadow", False))
    assert r["rules"]["action"] == "act" and r["amb"]["action"] == "act"
    assert r["ml"]["action"] == "escalate" and r["both"]["action"] == "escalate" and "disagrees" in r["ml"]["reason"]


def test_two_causes_at_once_escalate_only_where_the_ambiguity_rule_is_on():
    two = POOL + [alert("ContainerMemoryHigh", "transaction-service")]
    v = build_variants(two, correlate(two, False), {"verdict": "agree"}, "gate", True)
    r = rows(v)
    assert len(v["ambiguous_causes"]) == 2
    assert r["rules"]["action"] == "act" and r["ml"]["action"] == "act"
    assert r["amb"]["action"] == "escalate" and r["both"]["action"] == "escalate" and "together" in r["amb"]["reason"]
    assert r["both"]["active"] and not r["rules"]["active"]                                       # gate + ambiguity on is the 'both' row


def test_no_known_cause_escalates_in_every_row_and_no_opinion_is_reported_honestly():
    noise = [alert("SomethingElse", "frontend")]
    v = build_variants(noise, correlate(noise, False), None, "shadow", False)
    assert {x["action"] for x in v["rows"]} == {"escalate"} and v["second_opinion_verdict"] == "not consulted"


def test_the_webhook_records_the_variants_for_every_incident(monkeypatch):
    enable(monkeypatch, "shadow", fake("disagree"))
    monkeypatch.setattr(engine, "ESCALATE_ON_AMBIGUITY", False)
    from fastapi.testclient import TestClient
    body = TestClient(engine.app).post("/alerts", json={"alerts": POOL}).json()
    r = rows(body["variants"])
    assert body["status"] == "correlated" and r["rules"]["action"] == "act" and r["ml"]["action"] == "escalate"       # shown even though shadow mode acted
    assert engine.RECENT_INCIDENTS[-1]["variants"] == body["variants"]


def test_with_the_ambiguity_rule_on_the_blocked_incident_still_shows_what_the_rules_alone_would_do(monkeypatch):
    enable(monkeypatch, "shadow", fake("agree"))
    monkeypatch.setattr(engine, "ESCALATE_ON_AMBIGUITY", True)
    from fastapi.testclient import TestClient
    two = POOL + [alert("ContainerMemoryHigh", "transaction-service")]
    body = TestClient(engine.app).post("/alerts", json={"alerts": two}).json()
    r = rows(body["variants"])
    assert body["action"] == "escalate_to_human" and r["rules"]["action"] == "act" and r["amb"]["action"] == "escalate"
    assert body["variants"]["second_opinion_verdict"] == "agree"
