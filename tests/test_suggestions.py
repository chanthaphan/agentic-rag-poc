"""Follow-up chips: written from the turn that just happened, with the static list as the safety net."""
from pathlib import Path

import pytest

from bankrag.config import Settings
from bankrag import chat as C
from bankrag.skills import load_skills

ROOT = Path(__file__).resolve().parents[1]


class _Client:
    """Stands in for the chat model: records the call and replays a canned reply."""

    def __init__(self, text="", boom=False):
        self.text, self.boom, self.calls = text, boom, []

    def invoke(self, messages, **kw):
        self.calls.append(messages)
        if self.boom:
            raise RuntimeError("model down")
        from langchain_core.messages import AIMessage

        return AIMessage(content=self.text)


@pytest.fixture()
def settings():
    return Settings.load(ROOT)


def test_dynamic_suggestions_parse_and_clean():
    c = _Client('["ค่าธรรมเนียมรายปีเท่าไหร่คะ", "สมัครต้องใช้เอกสารอะไรบ้าง", "มีบัตรอื่นที่คล้ายกันไหม"]')
    got = C.dynamic_suggestions(c, "บัตรนี้ให้เลานจ์กี่ครั้ง", "ปีละ 2 ครั้งค่ะ", "th", "gpt-4.1-mini")
    assert got == ["ค่าธรรมเนียมรายปีเท่าไหร่คะ", "สมัครต้องใช้เอกสารอะไรบ้าง", "มีบัตรอื่นที่คล้ายกันไหม"]
    sent = " ".join(m.content for m in c.calls[0])
    assert "Thai" in sent and "ปีละ 2 ครั้ง" in sent  # the answer is the context, not just the question


def test_dynamic_suggestions_drop_repeats_and_junk():
    c = _Client('["  Already asked  ", "- A good new one", "x", "A good new one"]')
    got = C.dynamic_suggestions(c, "q", "a", "en", "m", asked=["already asked"])
    assert got == ["A good new one"]  # repeat dropped, bullet stripped, too-short dropped, duplicate dropped


def test_dynamic_suggestions_survive_bad_output():
    assert C.dynamic_suggestions(_Client("not json at all"), "q", "a", "en", "m") == []
    assert C.dynamic_suggestions(_Client("[oops"), "q", "a", "en", "m") == []
    assert C.dynamic_suggestions(_Client(boom=True), "q", "a", "en", "m") == []


def test_falls_back_to_the_static_list(settings):
    skills = load_skills(ROOT / "skills")
    spec = skills["credit-card"]
    trace = {}
    got = C.suggestions_for(settings, _Client(boom=True), spec, [], skills,
                            question="q", answer="an answer", language="th", trace=trace)
    assert got and got == C.pick_suggestions(spec, [], skills, language="th")
    assert "static" in trace["suggestions"]["mode"]


def test_static_mode_never_calls_the_model(settings):
    skills = load_skills(ROOT / "skills")
    settings.suggestions_mode = "static"
    c = _Client('["should not be used"]')
    trace = {}
    got = C.suggestions_for(settings, c, skills["credit-card"], [], skills,
                            question="q", answer="a", language="th", trace=trace)
    assert c.calls == [] and trace["suggestions"] == {"mode": "static"}
    assert got == C.pick_suggestions(skills["credit-card"], [], skills, language="th")


def test_dynamic_wins_when_it_works(settings):
    skills = load_skills(ROOT / "skills")
    settings.suggestions_mode = "dynamic"
    c = _Client('["ถามต่อข้อหนึ่ง", "ถามต่อข้อสอง", "ถามต่อข้อสาม"]')
    trace = {}
    got = C.suggestions_for(settings, c, skills["credit-card"], ["เดิม"], skills,
                            question="q", answer="a", language="th", trace=trace)
    assert got == ["ถามต่อข้อหนึ่ง", "ถามต่อข้อสอง", "ถามต่อข้อสาม"]
    assert trace["suggestions"]["mode"] == "dynamic" and trace["suggestions"]["ms"] >= 0


def test_an_empty_answer_does_not_trigger_a_call(settings):
    skills = load_skills(ROOT / "skills")
    c = _Client('["x"]')
    C.suggestions_for(settings, c, skills["credit-card"], [], skills, question="q", answer="   ", language="th", trace={})
    assert c.calls == []
