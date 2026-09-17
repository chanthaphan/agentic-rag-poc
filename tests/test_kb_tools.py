"""The knowledge-base MCP tool: which header it sends, how it calls the endpoint, how failures surface."""
from pathlib import Path

import pytest
from langchain_core.messages import ToolMessage

from bankrag import kb_tools as K
from bankrag.config import Settings

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def settings():
    s = Settings.load(ROOT)
    s.search_endpoint = "https://s.search.windows.net"
    s.search_query_key = "qk"
    s.search_api_version = "2026-08-01-preview"
    s.kb_transport = "mcp"  # these tests exercise the MCP transport; the default is rest
    return s


def test_headers_follow_the_auth_mode(settings, monkeypatch):
    monkeypatch.setattr(K, "token", lambda scope: f"tok-for-{scope}")
    settings.kb_mcp_auth = "identity"
    assert K.kb_headers(settings) == {"Authorization": "Bearer tok-for-https://search.azure.com/.default"}
    settings.kb_mcp_auth = "apikey"
    assert K.kb_headers(settings) == {"api-key": "qk"}
    assert K.kb_headers(settings, "identity")["Authorization"].startswith("Bearer ")


def test_tool_calls_the_endpoint_with_the_query_and_variants(settings, monkeypatch):
    seen = {}

    def fake_call(url, headers, name, arguments, timeout=60):
        seen.update(url=url, headers=headers, name=name, arguments=arguments)
        return "Retrieved 1 documents\n【1:0†Doc】 text"

    monkeypatch.setattr(K, "call_sync", fake_call)
    settings.kb_mcp_auth = "apikey"
    tool = K.kb_tool(settings, "kb-credit-card")
    out = tool.invoke({"query": "ค่าธรรมเนียม", "query_variants": ["annual fee", " "]})
    assert out.startswith("Retrieved 1") and seen["name"] == "knowledge_base_retrieve"
    assert seen["url"] == "https://s.search.windows.net/knowledgebases/kb-credit-card/mcp?api-version=2026-08-01-preview"
    assert seen["headers"] == {"api-key": "qk"} and seen["arguments"] == {"query": "ค่าธรรมเนียม", "query_variants": ["annual fee"]}
    assert tool.name == "knowledge_base_retrieve" and "query" in tool.args
    tool.invoke({"query": "q only"})
    assert seen["arguments"] == {"query": "q only", "query_variants": ["q only"]}  # the endpoint insists on an array


def test_failures_become_a_tool_error_message(settings, monkeypatch):
    def boom(*a, **kw):
        raise ConnectionError("no route")

    monkeypatch.setattr(K, "call_sync", boom)
    settings.kb_mcp_auth = "apikey"
    tool = K.kb_tool(settings, "kb-x")
    out = tool.invoke({"name": "knowledge_base_retrieve", "args": {"query": "q"}, "id": "c1", "type": "tool_call"})
    assert isinstance(out, ToolMessage) and out.status == "error" and "unavailable" in out.content and "no route" in out.content


def test_text_of_joins_text_parts_or_falls_back_to_structured():
    class C:
        def __init__(self, text=None):
            self.text = text

    class R:
        def __init__(self, content, structured=None):
            self.content, self.structured_content = content, structured

    assert K._text_of(R([C("a"), C(None), C("b")])) == "a\nb"
    assert K._text_of(R([], {"references": [1]})) == '{"references": [1]}'
    assert K._text_of(R([])) == ""


def test_rest_transport_passes_the_variants_as_intents(settings, monkeypatch):
    from bankrag import knowledge_base as KB
    from bankrag.models import Reference

    seen = {}

    def fake_retrieve(s, kb_name, question, *, ks_name=None, max_docs=None, variants=None):
        seen.update(kb=kb_name, q=question, ks=ks_name, max_docs=max_docs, variants=variants)
        return [Reference(id="a1", title="Doc A", source_url="https://www.bangkokbank.com/a", snippet="short", content="the whole chunk")]

    monkeypatch.setattr(KB, "retrieve", fake_retrieve)
    settings.kb_transport = "rest"
    tool = K.kb_tool(settings, "kb-credit-card", ks_name="ks-credit-card", top_k=5)
    out = tool.invoke({"query": "ค่าธรรมเนียม", "query_variants": ["annual fee"]})
    assert seen == {"kb": "kb-credit-card", "q": "ค่าธรรมเนียม", "ks": "ks-credit-card", "max_docs": 5, "variants": ["annual fee"]}
    assert out.startswith("Retrieved 1 documents") and "[1] Doc A" in out and "https://www.bangkokbank.com/a" in out and "the whole chunk" in out
    monkeypatch.setattr(KB, "retrieve", lambda *a, **kw: [])
    assert tool.invoke({"query": "nothing"}) == "Retrieved 0 documents"


def test_format_context_falls_back_to_the_snippet_and_caps():
    from bankrag.models import Reference

    assert K.format_context([]) == ""
    refs = [Reference(title="T", snippet="only a snippet"), Reference(title="U", source_url="https://x/u", content="x" * 50)]
    out = K.format_context(refs, max_chars=20)
    assert "Retrieved 2 documents" in out and "only a snippet" in out and "x" * 20 + " ..." in out and "x" * 21 not in out and "https://x/u" in out
