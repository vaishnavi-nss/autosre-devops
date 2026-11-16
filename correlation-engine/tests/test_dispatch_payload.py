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
