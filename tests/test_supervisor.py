"""Supervisor mode: the concierge hands off in-process and the specialist streams the answer."""
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from bankrag import chat as C
from bankrag.config import Settings
from bankrag.skills import load_skills
from bankrag.supervisor import concierge_definition, handoff_tool_name, skill_id_of
from fakes import FakeAgentModel, ai, fake_kb_tool

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def settings(tmp_path):
    s = Settings.load(ROOT)
    s.state_dir = tmp_path / ".state"
    s.search_endpoint = "https://s.search.windows.net"
    s.suggestions_mode = "static"
    s.orchestration_mode = "supervisor"
    return s


def test_concierge_definition_lists_one_handoff_per_skill(settings):
    skills = load_skills(ROOT / "skills")
    d = concierge_definition(settings, skills)
    assert len(d.tools) == len(skills) and all(t["type"] == "handoff" for t in d.tools)
    assert handoff_tool_name("credit-card") == "handoff_to_credit_card" and skill_id_of("handoff_to_credit_card", skills) == "credit-card"
    assert skill_id_of("knowledge_base_retrieve", skills) == "" and "A2A" not in d.instructions


def _session(settings, monkeypatch, replies):
    from bankrag import tools as T

    skills = load_skills(ROOT / "skills")
    monkeypatch.setattr(C, "synced_kb_owners", lambda s, sk: dict(sk))
    calls = []
    monkeypatch.setattr(T, "kb_tool", lambda settings, kb_name, **kw: fake_kb_tool(calls=calls))
    model = FakeAgentModel(responses=replies)
    return C.ChatSession(settings, skills, llm_factory=lambda m: model, checkpointer=InMemorySaver()), model, calls


def test_handoff_runs_the_specialist_and_streams_its_answer(settings, monkeypatch):
    session, model, kb_calls = _session(settings, monkeypatch, [
        ai(tool_calls=[{"name": "handoff_to_credit_card", "args": {"question": "ค่าธรรมเนียม Visa Platinum เท่าไหร่", "context": ""}, "id": "h1"}], usage={"input_tokens": 50, "output_tokens": 10}),
        ai(tool_calls=[{"name": "knowledge_base_retrieve", "args": {"query": "ค่าธรรมเนียม Visa Platinum"}, "id": "c1"}], usage={"input_tokens": 200, "output_tokens": 20}),
        ai("ค่าธรรมเนียมรายปี 3,000 บาทค่ะ【1:0†Bangkok Bank Visa Platinum】", usage={"input_tokens": 400, "output_tokens": 30}),
    ])
    monkeypatch.setattr(C, "_lookup_title", lambda s, t, c: "https://www.bangkokbank.com/platinum")
    events = list(session.ask_stream("ค่าธรรมเนียม Visa Platinum เท่าไหร่", with_sources=False))
    types = [e["type"] for e in events]
    assert types[:3] == ["route", "conversation", "status"] and events[0]["skill_id"] == "concierge" and types[-1] == "done"
    phases = [e["phase"] for e in events if e["type"] == "status"]
    assert phases[0] == "choosing" and "specialist" in phases and "retrieving" in phases
    tools = [e["name"] for e in events if e["type"] == "tool"]
    assert tools == ["handoff_to_credit_card", "knowledge_base_retrieve"]
    assert "".join(e["text"] for e in events if e["type"] == "delta").startswith("ค่าธรรมเนียมรายปี 3,000 บาทค่ะ")  # the specialist streamed
    ans = events[-1]["answer"]
    assert ans.skill_id == "credit-card" and ans.agent_name == "bank-concierge" and ans.text.startswith("ค่าธรรมเนียมรายปี 3,000 บาทค่ะ")
    tr = ans.trace
    assert tr["handoff"]["mode"] == "supervisor" and tr["handoff"]["specialist"] == "credit-card" and tr["handoff"]["usage_pending"] is False
    assert tr["usage"]["agent"]["input_tokens"] == 50 and tr["usage"]["specialist"]["input_tokens"] == 600 and tr["usage"]["total"]["input_tokens"] == 650
    assert "specialist" in tr["cost"] and tr["retrieval"]["calls"] == 1 and kb_calls[0]["query"] == "ค่าธรรมเนียม Visa Platinum"
    assert [c.url for c in ans.citations] == ["https://www.bangkokbank.com/platinum"]
    assert session.prev_skill == "credit-card"
    # the specialist saw its own skill instructions, not the concierge's
    spec_prompt = model.calls[1][0].content
    assert "Skill: " in spec_prompt and "handoff tool" not in spec_prompt
    # the thread keeps the question and the specialist's answer only
    graph = next(g for k, g in session._graphs.items() if k[0] == "__concierge__")
    kinds = [type(m).__name__ for m in graph.get_state(session._config()).values["messages"]]
    assert kinds == ["HumanMessage", "AIMessage"]


def test_concierge_answers_small_talk_itself(settings, monkeypatch):
    session, model, _ = _session(settings, monkeypatch, [ai("สวัสดีค่ะ เกรสยินดีให้บริการค่ะ", usage={"input_tokens": 30, "output_tokens": 8})])
    events = list(session.ask_stream("สวัสดี", with_sources=False))
    ans = events[-1]["answer"]
    assert ans.skill_id == "concierge" and ans.text == "สวัสดีค่ะ เกรสยินดีให้บริการค่ะ" and ans.route_reason.endswith("without a handoff")
    assert ans.trace["usage"]["specialist"] == {} and ans.trace["handoff"]["specialist"] == "concierge"
    assert "".join(e["text"] for e in events if e["type"] == "delta") == ans.text
