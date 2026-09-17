"""A full router-mode turn through the real ChatSession: fake model, fake knowledge-base tool, in-memory thread."""
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from bankrag import chat as C
from bankrag.config import Settings
from bankrag.models import Reference, RouteDecision
from bankrag.skills import load_skills
from fakes import KB_OUTPUT, FakeAgentModel, ai, fake_kb_tool

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def settings(tmp_path):
    s = Settings.load(ROOT)
    s.state_dir = tmp_path / ".state"
    s.search_endpoint = "https://s.search.windows.net"
    s.suggestions_mode = "static"
    return s


def _session(settings, monkeypatch, model, kb_calls=None, kb_outputs=None, fail_kb=False):
    from bankrag import tools as T

    skills = load_skills(ROOT / "skills")
    monkeypatch.setattr(C, "synced_kb_owners", lambda s, sk: dict(sk))
    monkeypatch.setattr(T, "kb_tool", lambda settings, kb_name, **kw: fake_kb_tool(kb_outputs, calls=kb_calls, fail=fail_kb))
    session = C.ChatSession(settings, skills, llm_factory=lambda m: model, checkpointer=InMemorySaver())
    monkeypatch.setattr(session, "decide", lambda q, force=None: RouteDecision(skill_id="credit-card", confidence=0.9, language="th", reason="test", usage={"input_tokens": 10, "output_tokens": 5}))
    return session


def test_turn_with_retrieval_events_trace_and_citations(settings, monkeypatch):
    calls = []
    model = FakeAgentModel(responses=[
        ai(tool_calls=[{"name": "knowledge_base_retrieve", "args": {"query": "ค่าธรรมเนียม Visa Platinum", "query_variants": ["Platinum annual fee"]}, "id": "call_1"}], usage={"input_tokens": 100, "output_tokens": 20}),
        ai("ค่าธรรมเนียมรายปี 3,000 บาทค่ะ【1:0†Bangkok Bank Visa Platinum】", usage={"input_tokens": 300, "output_tokens": 40}, id="resp_9"),
    ])
    session = _session(settings, monkeypatch, model, kb_calls=calls)
    monkeypatch.setattr(C, "_lookup_title", lambda s, t, c: "https://www.bangkokbank.com/platinum" if "Platinum" in t else "")
    events = list(session.ask_stream("ค่าธรรมเนียม Visa Platinum เท่าไหร่", with_sources=False))
    types = [e["type"] for e in events]
    assert types[:3] == ["route", "conversation", "status"] and types[-1] == "done"
    assert "tool" in types and types.index("tool") < types.index("done")
    tool_ev = next(e for e in events if e["type"] == "tool")
    assert tool_ev["name"] == "knowledge_base_retrieve" and "Platinum" in tool_ev["arguments"] and tool_ev["error"] == ""
    phases = [e["phase"] for e in events if e["type"] == "status"]
    assert phases[0] == "drafting" and "retrieving" in phases and phases[-1] == "drafting"
    assert calls == [{"query": "ค่าธรรมเนียม Visa Platinum", "query_variants": ["Platinum annual fee"]}]
    ans = events[-1]["answer"]
    assert ans.text == "ค่าธรรมเนียมรายปี 3,000 บาทค่ะ" or ans.text.startswith("ค่าธรรมเนียมรายปี 3,000 บาทค่ะ")  # markers gone; a warning may follow
    assert ans.skill_id == "credit-card" and ans.agent_name == "bank-credit-card" and ans.conversation_id.startswith("thr_")
    tr = ans.trace
    assert tr["retrieval"]["calls"] == 1 and tr["retrieval"]["documents"] == 2 and tr["retrieval"]["query_variants"] == ["Platinum annual fee"]
    assert tr["retrieval"]["output_tokens"] > 0 and tr["retrieval"]["output_chars"] == len(KB_OUTPUT)
    assert tr["usage"]["agent"] == {"input_tokens": 400, "output_tokens": 60, "total_tokens": 460, "cached_tokens": 0, "reasoning_tokens": 0}
    assert tr["usage"]["total"]["input_tokens"] == 410 and tr["usage"]["router"]["input_tokens"] == 10
    assert tr["model"] == "gpt-4.1-mini" and tr["response_id"] == "resp_9" and tr["cost"]["total_usd"] >= 0
    assert tr["conversation"] == {"id": ans.conversation_id, "turn": 1, "rotated": False, "trimmed": 0}
    assert ans.retrieval_context == [KB_OUTPUT]
    assert [tc["type"] for tc in ans.tool_calls] == ["mcp_call"] and "Retrieved 2" in ans.tool_calls[0]["output"]
    assert [(c.title, c.url) for c in ans.citations] == [("Bangkok Bank Visa Platinum", "https://www.bangkokbank.com/platinum")]
    # the thread keeps only the question and the answer: the tool traffic was compacted away
    state = session._graphs[next(iter(session._graphs))].get_state(session._config())
    kinds = [type(m).__name__ for m in state.values["messages"]]
    assert kinds == ["HumanMessage", "AIMessage"] and not state.values["messages"][1].tool_calls
    # the system prompt carried the published instructions and the reply-language note
    first_prompt = model.calls[0]
    assert first_prompt[0].type == "system" and "Reply-language note: the customer wrote in Thai" in first_prompt[0].content
    assert isinstance(first_prompt[-1], HumanMessage) and first_prompt[-1].content == "ค่าธรรมเนียม Visa Platinum เท่าไหร่"


def test_follow_up_sees_history_and_the_window_trims(settings, monkeypatch):
    settings.history_turns = 1
    model = FakeAgentModel(responses=[ai("a1"), ai("a2"), ai("a3")])
    session = _session(settings, monkeypatch, model)
    for q in ("q1", "q2", "q3"):
        ans = session.ask(q, with_sources=False)
    assert ans.trace["conversation"]["turn"] == 3 and ans.trace["conversation"]["trimmed"] == 1
    third = model.calls[2]
    assert [m.type for m in third] == ["system", "system", "human", "ai", "human"]  # system, recap, q2, a2, q3
    assert "q1" in third[1].content and [m.content for m in third[2:]] == ["q2", "a2", "q3"]
    assert session.history[-1] == {"role": "assistant", "content": "a3"} and session.prev_skill == "credit-card"


def test_tool_error_still_answers_and_is_traced(settings, monkeypatch):
    model = FakeAgentModel(responses=[
        ai(tool_calls=[{"name": "knowledge_base_retrieve", "args": {"query": "q"}, "id": "c1"}]),
        ai("ยังไม่มีรายละเอียดเรื่องนี้ให้แนะนำค่ะ"),
    ])
    session = _session(settings, monkeypatch, model, fail_kb=True)
    events = list(session.ask_stream("q", with_sources=False))
    tool_ev = next(e for e in events if e["type"] == "tool")
    assert "unavailable" in tool_ev["error"]
    ans = events[-1]["answer"]
    assert ans.text.startswith("ยังไม่มีรายละเอียด") and ans.tool_calls[0]["error"] and ans.retrieval_context == []
    assert ans.trace["retrieval"]["calls"] == 1 and ans.trace["retrieval"]["documents"] == 0


def test_tool_rounds_are_capped(settings, monkeypatch):
    calls = []
    looping = [ai(tool_calls=[{"name": "knowledge_base_retrieve", "args": {"query": f"q{i}"}, "id": f"c{i}"}]) for i in range(10)]
    model = FakeAgentModel(responses=looping + [ai("final")])
    session = _session(settings, monkeypatch, model, kb_calls=calls)
    ans = session.ask("loop", with_sources=False)
    assert ans.text == "final" and len(calls) == C.MAX_TOOL_ROUNDS and ans.trace["retrieval"]["calls"] == C.MAX_TOOL_ROUNDS


def test_reset_and_rehydration(settings, monkeypatch):
    from bankrag.models import SessionRecord
    from bankrag.sessions import append_turns, new_record

    model = FakeAgentModel(responses=[ai("a1"), ai("a2")])
    session = _session(settings, monkeypatch, model)
    session.ask("q1", with_sources=False)
    thread = session.conversation_id
    saver = session.checkpointer
    assert saver.get_tuple({"configurable": {"thread_id": thread}}) is not None
    session.reset()
    assert session.conversation_id is None and session.history == [] and saver.get_tuple({"configurable": {"thread_id": thread}}) is None
    # a record from before the LangGraph memory (a Foundry conversation id): the thread is rebuilt from the stored turns
    rec = new_record(); rec.conversation_id = "conv_legacy"
    append_turns(rec, "q0", C.Answer(skill_id="credit-card", confidence=1.0, text="a0"))
    restored = C.ChatSession.from_record(settings, load_skills(ROOT / "skills"), rec, llm_factory=lambda m: model, checkpointer=InMemorySaver())
    monkeypatch.setattr(restored, "decide", lambda q, force=None: RouteDecision(skill_id="credit-card", confidence=0.9, language="th", reason="t"))
    ans = restored.ask("q1", with_sources=False)
    assert restored.conversation_id.startswith("thr_") and ans.conversation_id == restored.conversation_id
    assert [m.content for m in model.calls[-1][1:]] == ["q0", "a0", "q1"]


def test_offtopic_never_touches_the_graph(settings, monkeypatch):
    model = FakeAgentModel(responses=[])
    session = _session(settings, monkeypatch, model)
    monkeypatch.setattr(session, "decide", lambda q, force=None: RouteDecision(skill_id="offtopic", confidence=0.9, language="en", reason="t"))
    events = list(session.ask_stream("tell me a joke", with_sources=False))
    assert [e["type"] for e in events] == ["route", "delta", "done"] and events[-1]["answer"].skill_id == "offtopic" and model.calls == []


REAL_KB_OUTPUT = (
    '[{"ref_id":0,"title":"บัตรเครดิตวีซ่า แพลทินัม","terms":"t","content":"ค่าธรรมเนียมรายปี 3,000 บาท"},'
    '{"ref_id":1,"title":"บัตรอินฟินิท","terms":"t","content":"เลานจ์ 2 ครั้ง"}]\n'
    '{"kind":"reference","ref_id":"0","uri":"https://s.search.windows.net/indexes/bank-products/docs/aaa1?x=1","mimeType":"application/json",'
    '"sourceData":{"id":"aaa1","title":"บัตรเครดิตวีซ่า แพลทินัม","source_url":"https://www.bangkokbank.com/platinum"}}\n'
    '{"kind":"reference","ref_id":"1","uri":"https://s.search.windows.net/indexes/bank-products/docs/bbb2?x=1","mimeType":"application/json",'
    '"sourceData":"{\'id\': \'bbb2\', \'source_url\': \'https://www.bangkokbank.com/infinite\'}"}'
)


def test_real_kb_output_shape_counts_documents_and_resolves_citations(settings, monkeypatch):
    from bankrag.graph import references_in_outputs

    refs = references_in_outputs([REAL_KB_OUTPUT])
    assert [(r["id"], r["title"], r["source_url"]) for r in refs] == [
        ("aaa1", "บัตรเครดิตวีซ่า แพลทินัม", "https://www.bangkokbank.com/platinum"), ("bbb2", "บัตรอินฟินิท", "https://www.bangkokbank.com/infinite")]
    model = FakeAgentModel(responses=[
        ai(tool_calls=[{"name": "knowledge_base_retrieve", "args": {"query": "q"}, "id": "c1"}]),
        ai("ค่าธรรมเนียม 3,000 บาทค่ะ [บัตรเครดิตวีซ่า แพลทินัม](https://s.search.windows.net/indexes/bank-products/docs/aaa1?x=1) และเลานจ์【1:1†บัตรอินฟินิท】"),
    ])
    session = _session(settings, monkeypatch, model, kb_outputs=[REAL_KB_OUTPUT])
    monkeypatch.setattr(C, "_lookup_docs", lambda s, ids: {})
    ans = session.ask("q", with_sources=False)
    assert ans.trace["retrieval"]["documents"] == 2
    assert [(c.title, c.url) for c in ans.citations] == [("บัตรอินฟินิท", "https://www.bangkokbank.com/infinite"), ("บัตรเครดิตวีซ่า แพลทินัม", "https://www.bangkokbank.com/platinum")]
