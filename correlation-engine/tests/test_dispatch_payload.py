"""What the engine hands the dispatcher: the cause and confidence (as before) plus the diagnosed service and the second-opinion
verdict, so the dispatcher can require agreement and refuse a mismatched target."""
import asyncio


class _Reply:
    def raise_for_status(self):
        pass

    def json(self):
        return {"status": "dry_run"}


def _fake_client(sent):
    class Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json):
            sent.append((url, json))
            return _Reply()
    return Client


RESULT = {"root_cause": "db_connection_pool_exhaustion", "service": "transaction-service", "confidence": 0.9,
          "playbook": "restart_service.yml"}


def test_the_payload_carries_service_and_verdict(monkeypatch):
    import app as engine
    sent = []
    monkeypatch.setattr(engine, "DISPATCHER_ENABLED", True)
    monkeypatch.setattr(engine, "DISPATCHER_URL", "http://dispatcher/dispatch")
    monkeypatch.setattr(engine.httpx, "AsyncClient", _fake_client(sent))
    opinion = {"verdict": "agree", "scorer_service": "transaction-service"}
    out = asyncio.run(engine._dispatch_remediation(RESULT, opinion))
    assert out == {"status": "dry_run"}
    url, body = sent[0]
    assert url == "http://dispatcher/dispatch"
    assert body == {"root_cause": "db_connection_pool_exhaustion", "confidence": 0.9, "service": "transaction-service",
                    "playbook": "restart_service.yml", "second_opinion": opinion}


def test_without_a_scorer_the_verdict_is_null_and_the_old_call_shape_still_works(monkeypatch):
    import app as engine
    sent = []
    monkeypatch.setattr(engine, "DISPATCHER_ENABLED", True)
    monkeypatch.setattr(engine, "DISPATCHER_URL", "http://dispatcher/dispatch")
    monkeypatch.setattr(engine.httpx, "AsyncClient", _fake_client(sent))
    asyncio.run(engine._dispatch_remediation(RESULT))
    assert sent[0][1]["second_opinion"] is None and sent[0][1]["root_cause"] == RESULT["root_cause"]


def test_a_disabled_or_unconfigured_dispatcher_never_blocks_correlation(monkeypatch):
    import app as engine
    monkeypatch.setattr(engine, "DISPATCHER_ENABLED", False)
    assert asyncio.run(engine._dispatch_remediation(RESULT, None))["status"] == "disabled"
    monkeypatch.setattr(engine, "DISPATCHER_ENABLED", True)
    monkeypatch.setattr(engine, "DISPATCHER_URL", None)
    assert asyncio.run(engine._dispatch_remediation(RESULT, None))["status"] == "unavailable"


# ---------------------------------------------------------------------------------------------------- evidence for a person
def _alert(name, service):
    return {"status": "firing", "labels": {"alertname": name, "service": service, "severity": "warning"}}


class _DiagReply:
    def __init__(self, data):
        self.data = data

    def json(self):
        return self.data


def _diag_client(sent, reply):
    class Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json):
            sent.append((url, json))
            if isinstance(reply, Exception):
                raise reply
            return _DiagReply(reply)
    return Client


def test_evidence_is_requested_for_the_deepest_alerting_service(monkeypatch):
    import app as engine
    sent = []
    monkeypatch.setattr(engine, "DISPATCHER_ENABLED", True)
    monkeypatch.setattr(engine, "DISPATCHER_URL", "http://dispatcher:8001/dispatch")
    monkeypatch.setattr(engine.httpx, "AsyncClient", _diag_client(sent, {"status": "accepted", "execution_id": "abc123"}))
    out = asyncio.run(engine._request_diagnostics([_alert("ErrorRateHigh", "api-gateway"), _alert("PodRestartsHigh", "transaction-service")], "no known cause"))
    assert out == {"execution_id": "abc123", "service": "transaction-service", "status": "accepted"}
    url, body = sent[0]
    assert url == "http://dispatcher:8001/diagnostics" and body == {"service": "transaction-service", "reason": "no known cause", "alerts": ["ErrorRateHigh", "PodRestartsHigh"]}


def test_evidence_never_blocks_or_raises(monkeypatch):
    import app as engine
    monkeypatch.setattr(engine, "DISPATCHER_ENABLED", True)
    monkeypatch.setattr(engine, "DISPATCHER_URL", "http://dispatcher:8001/dispatch")
    monkeypatch.setattr(engine.httpx, "AsyncClient", _diag_client([], RuntimeError("down")))
    assert asyncio.run(engine._request_diagnostics([_alert("ErrorRateHigh", "api-gateway")], "x")) is None
    monkeypatch.setattr(engine, "DISPATCHER_ENABLED", False)
    assert asyncio.run(engine._request_diagnostics([_alert("ErrorRateHigh", "api-gateway")], "x")) is None
    monkeypatch.setattr(engine, "DISPATCHER_ENABLED", True)
    assert asyncio.run(engine._request_diagnostics([_alert("SomethingElse", "not-a-service")], "x")) is None        # no service in the chain to look at


def test_an_unmatched_incident_records_which_evidence_was_requested(client, monkeypatch):
    import app as engine
    sent = []
    engine.RECENT_INCIDENTS.clear()
    monkeypatch.setattr(engine, "SECOND_OPINION_URL", None)
    monkeypatch.setattr(engine, "DISPATCHER_ENABLED", True)
    monkeypatch.setattr(engine, "DISPATCHER_URL", "http://dispatcher:8001/dispatch")
    monkeypatch.setattr(engine.httpx, "AsyncClient", _diag_client(sent, {"status": "accepted", "execution_id": "e1"}))
    body = client.post("/alerts", json={"alerts": [_alert("ErrorRateHigh", "api-gateway"), _alert("APILatencyHigh", "api-gateway")]}).json()
    assert body["status"] == "unmatched" and body["diagnostics"]["execution_id"] == "e1"
    assert client.get("/incidents").json()["incidents"][0]["diagnostics"]["execution_id"] == "e1"
    assert [u for u, _ in sent] == ["http://dispatcher:8001/diagnostics"]                          # no fix was asked for, only evidence
