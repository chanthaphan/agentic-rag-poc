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
