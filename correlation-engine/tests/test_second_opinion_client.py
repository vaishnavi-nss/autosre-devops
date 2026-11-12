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
