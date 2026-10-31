"""The engine's side of the second opinion: asking, annotating, and (in gate mode) vetoing.

The scorer service is faked; what matters here is that it can never make an incident worse.
"""

import app as engine
import second_opinion as so


def fake(verdict, reason="because"):
    calls = []

    def ask(url, cause, service, started, timeout):
        calls.append((url, cause, service, started))
        return {"verdict": verdict, "reason": reason}

    ask.calls = calls
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
