import json

from fastapi.testclient import TestClient

from bankrag import api
from bankrag.models import Answer


class StubSession:
    def __init__(self, settings, skills, project=None):
        self.settings, self.skills, self.conversation_id, self.history, self.prev_skill = settings, skills, None, [], None

    @classmethod
    def from_record(cls, settings, skills, rec, project=None):
        return cls(settings, skills)

    def to_record(self, rec):
        rec.conversation_id, rec.prev_skill = "conv_x", "credit-card"
        return rec

    def ask_stream(self, q, force_skill=None, with_sources=True):
        yield {"type": "route", "skill_id": "credit-card", "confidence": 0.9, "language": "th", "reason": "r", "agent_name": "bank-credit-card", "route_ms": 5}
        yield {"type": "delta", "text": "สวัส"}
        yield {"type": "delta", "text": "ดี"}
        yield {"type": "tool", "name": "knowledge_base_retrieve", "arguments": "{}", "error": ""}
        yield {"type": "done", "answer": Answer(skill_id="credit-card", confidence=0.9, text="สวัสดี", language="th", suggestions=["x"])}

    def reset(self):
        pass


def test_chat_stream_events_and_persistence(tmp_path, monkeypatch):
    import bankrag.chat as chat_mod

    monkeypatch.setattr(api.settings, "state_dir", tmp_path / ".state")
    monkeypatch.setattr(chat_mod, "ChatSession", StubSession)
    api._sessions.clear()
    c = TestClient(api.app)
    with c.stream("POST", "/chat/stream", json={"message": "สวัสดี", "source": "studio"}) as r:
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        events = [json.loads(line[6:]) for line in r.iter_lines() if line.startswith("data: ")]
    types = [e["type"] for e in events]
    assert types == ["session", "route", "delta", "delta", "tool", "done"]
    sid = events[0]["session_id"]
    assert events[-1]["answer"]["text"] == "สวัสดี" and events[-1]["title"] == "สวัสดี"
    rec = c.get(f"/sessions/{sid}").json()
    assert len(rec["turns"]) == 2 and rec["conversation_id"] == "conv_x" and rec["source"] == "studio"
