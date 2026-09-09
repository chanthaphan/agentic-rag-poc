import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from bankrag.config import Settings
from bankrag import mcp_server as MS
from bankrag import services as SV

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def settings():
    s = Settings.load(ROOT)
    s.bbl_api_subscription = "test-key"
    s.services_mcp_key = "tool-secret"
    return s


def _rpc(body: dict, key: str | None, settings) -> "object":
    app, _inner = MS.build_asgi(settings)
    headers = {"content-type": "application/json", "accept": "application/json, text/event-stream"}
    if key is not None:
        headers[MS.AUTH_HEADER] = key
    with TestClient(app) as client:  # the context manager runs the lifespan the transport needs
        return client.post("/", json=body, headers=headers)


INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "test", "version": "1"}}}


def test_the_endpoint_is_closed_without_the_tool_key(settings):
    assert _rpc(INIT, None, settings).status_code == 401
    assert _rpc(INIT, "wrong", settings).status_code == 401


def test_the_endpoint_answers_with_the_tool_key(settings):
    r = _rpc(INIT, "tool-secret", settings)
    assert r.status_code == 200, r.text
    assert "bank-services" in r.text


def test_fx_tool_is_registered(settings):
    server = MS.build_server(settings)
    import anyio

    tools = anyio.run(server.list_tools)
    names = [t.name for t in tools]
    assert MS.TOOL_FX in names
    fx = next(t for t in tools if t.name == MS.TOOL_FX)
    assert "as_of" in (fx.description or "") and "currency" in json.dumps(fx.input_schema)


def test_fx_tool_returns_the_rate(settings, monkeypatch):
    monkeypatch.setattr(SV, "fx_rate", lambda s, c: {"found": True, "currency": c.upper(), "buying": 35.5, "as_of": "now"})
    server = MS.build_server(settings)
    import anyio

    out = anyio.run(lambda: server.call_tool(MS.TOOL_FX, {"currency": "usd"}))
    assert "35.5" in json.dumps(out, default=str) and "USD" in json.dumps(out, default=str)


def test_fx_tool_degrades_when_the_service_is_down(settings, monkeypatch):
    def boom(s, c):
        raise SV.ServiceError("401 from the bank API")
    monkeypatch.setattr(SV, "fx_rate", boom)
    server = MS.build_server(settings)
    import anyio

    payload = json.dumps(anyio.run(lambda: server.call_tool(MS.TOOL_FX, {"currency": "USD"})), default=str)
    assert "not available" in payload  # the agent gets a sentence to say, not a stack trace
    assert "found" in payload


# ---- who is allowed to call the tools ----
def _principal_header(oid: str) -> str:
    import base64 as b64

    claims = {"claims": [{"typ": "http://schemas.microsoft.com/identity/claims/objectidentifier", "val": oid}]}
    return b64.b64encode(json.dumps(claims).encode()).decode()


def test_azure_pins_the_caller_to_the_foundry_identity(settings):
    """Easy Auth proves a tenant identity; only the project's managed identity may call the tools."""
    settings.services_mcp_callers = ["11111111-2222-3333-4444-555555555555"]
    settings.services_mcp_key = ""  # the key guard is not in play once identities are configured
    app, _ = MS.build_asgi(settings)
    with TestClient(app) as client:
        head = {"content-type": "application/json", "accept": "application/json, text/event-stream"}
        assert client.post("/", json=INIT, headers=head).status_code == 401  # no Easy Auth headers at all
        wrong = dict(head, **{"x-ms-client-principal-id": "99999999-0000-0000-0000-000000000000"})
        assert client.post("/", json=INIT, headers=wrong).status_code == 401
        ok = dict(head, **{"x-ms-client-principal-id": "11111111-2222-3333-4444-555555555555"})
        assert client.post("/", json=INIT, headers=ok).status_code == 200


def test_object_id_is_read_from_either_easy_auth_header(settings):
    assert MS.caller_object_id({"x-ms-client-principal-id": "abc"}) == "abc"
    assert MS.caller_object_id({"x-ms-client-principal": _principal_header("from-claims")}) == "from-claims"
    assert MS.caller_object_id({}) == ""
    assert MS.caller_object_id({"x-ms-client-principal": "not-base64!!"}) == ""


def test_closed_when_nothing_is_configured(settings):
    settings.services_mcp_callers = []
    settings.services_mcp_key = ""
    app, _ = MS.build_asgi(settings)
    with TestClient(app) as client:
        r = client.post("/", json=INIT, headers={"content-type": "application/json", "accept": "application/json, text/event-stream"})
        assert r.status_code == 401 and "not configured" in r.text
