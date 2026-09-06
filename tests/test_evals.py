from pathlib import Path

from bankrag.config import Settings
from bankrag.evals import load_cases, run_rag, run_routing, save_cases
from bankrag.models import Answer, Citation
from bankrag.skills import load_skills

ROOT = Path(__file__).resolve().parents[1]


class FakeResp:
    def __init__(self, text):
        self.output_text = text
        self.usage = None


class FakeResponses:
    def create(self, **kw):
        q = kw["input"]
        skill = "credit-card" if "บัตรเครดิต" in q or "credit" in q.lower() else "offtopic" if "อากาศ" in q else "general"
        return FakeResp(f'{{"skill_id": "{skill}", "confidence": 0.9, "language": "th", "reason": "stub"}}')


class FakeOpenAI:
    responses = FakeResponses()


def test_run_routing_with_stub_router(tmp_path):
    s = Settings.load(ROOT)
    skills = load_skills(ROOT / "skills")
    cases = [{"q": "บัตรเครดิตใบไหนดี", "skill": "credit-card"}, {"q": "วันนี้อากาศดีไหม", "skill": "offtopic"}, {"q": "อยากซื้อประกัน", "skill": "insurance"}]
    run = run_routing(s, skills, cases, openai_client=FakeOpenAI(), log=lambda m: None)
    assert run["summary"]["passed"] == 2 and round(run["summary"]["accuracy"], 2) == 0.67 and run["rows"][2]["got"] == "general"


def test_run_rag_with_stub_session(tmp_path):
    s = Settings.load(ROOT)
    skills = load_skills(ROOT / "skills")

    class StubSession:
        def ask(self, q, force_skill=None):
            return Answer(skill_id=force_skill or "credit-card", confidence=0.9, text="ค่าธรรมเนียม 3,000 บาท", citations=[Citation(url="https://x")], trace={"cost": {"total_usd": 0.001}})

        def reset(self):
            pass

    cases = [{"q": "ค่าธรรมเนียม?", "expect": ["3,000"]}, {"q": "อื่นๆ", "expect": ["ไม่มี"], "require_source": False}]
    run = run_rag(s, skills, cases, session_factory=StubSession, log=lambda m: None)
    assert run["summary"]["passed"] == 1 and run["rows"][1]["pass"] is False and run["summary"]["total_cost_usd"] == 0.002


def test_save_and_load_cases(tmp_path):
    s = Settings.load(ROOT)
    s.root = tmp_path
    saved = save_cases(s, "routing", [{"q": " hi ", "skill": "general"}, {"q": "", "skill": "x"}])
    assert saved == [{"q": "hi", "skill": "general"}] and load_cases(s, "routing") == saved
    saved = save_cases(s, "rag", [{"q": "q", "expect": ["a", ""], "skill": "credit-card", "require_source": False}])
    assert saved[0] == {"q": "q", "expect": ["a"], "skill": "credit-card", "require_source": False}
