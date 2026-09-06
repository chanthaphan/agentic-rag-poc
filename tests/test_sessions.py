from pathlib import Path

from bankrag.chat import ChatSession, detect_language, pick_suggestions, strip_markers
from bankrag.config import Settings
from bankrag.models import Answer, Citation
from bankrag.sessions import append_turns, delete_session, list_sessions, load_session, new_record, save_session
from bankrag.skills import load_skills

ROOT = Path(__file__).resolve().parents[1]


class FakeOpenAI:
    pass


class FakeProject:
    def get_openai_client(self):
        return FakeOpenAI()


def _settings(tmp_path):
    s = Settings.load(ROOT)
    s.state_dir = tmp_path / ".state"
    return s


def test_session_roundtrip_and_rehydration(tmp_path):
    s = _settings(tmp_path)
    rec = new_record()
    ans = Answer(skill_id="credit-card", confidence=0.9, text="answer", citations=[Citation(title="t", url="https://x")], suggestions=["a", "b"])
    append_turns(rec, "บัตรอินฟินิทเข้าเลานจ์ได้กี่ครั้ง", ans)
    rec.conversation_id = "conv_123"
    rec.prev_skill = "credit-card"
    save_session(s, rec)
    assert list_sessions(s)[0]["id"] == rec.id and list_sessions(s)[0]["turns"] == 2
    loaded = load_session(s, rec.id)
    assert loaded.title.startswith("บัตรอินฟินิท") and loaded.turns[1].citations[0].url == "https://x"
    skills = load_skills(ROOT / "skills")
    cs = ChatSession.from_record(s, skills, loaded, project=FakeProject())
    assert cs.conversation_id == "conv_123" and cs.prev_skill == "credit-card"
    assert [h["role"] for h in cs.history] == ["user", "assistant"]
    from bankrag.sessions import stats

    st = stats(s)
    assert st["sessions"] == 1 and st["answers"] == 1 and st["per_skill"] == {"credit-card": 1}
    assert (s.state_dir / "bankrag.db").exists()
    assert delete_session(s, rec.id) and load_session(s, rec.id) is None


def test_pick_suggestions_and_markers():
    skills = load_skills(ROOT / "skills")
    cc = skills["credit-card"]
    out = pick_suggestions(cc, [cc.suggestions[0]], skills)
    assert len(out) == 3 and cc.suggestions[0] not in out
    assert strip_markers("ค่าธรรมเนียม 3,000 บาท【4:0†source】 ต่อปี【4:1】") == "ค่าธรรมเนียม 3,000 บาท ต่อปี"
    assert strip_markers("สวัสดีค่ะ มีอะไรให้ช่วยไหมคะ (โปรดตอบเป็นภาษาไทย)") == "สวัสดีค่ะ มีอะไรให้ช่วยไหมคะ"


def test_language_detection_and_language_filtered_suggestions():
    assert detect_language("บัตร Infinite เข้าเลานจ์ได้กี่ครั้ง") == "th"
    assert detect_language("What is the annual fee of the Visa Platinum card?") == "en"
    skills = load_skills(ROOT / "skills")
    for sid, spec in skills.items():
        th = pick_suggestions(spec, [], skills, language="th")
        en = pick_suggestions(spec, [], skills, language="en")
        assert len(th) == 3 and all(detect_language(x) == "th" for x in th), sid
        assert len(en) == 3 and all(detect_language(x) == "en" for x in en), sid


def test_recap_items_and_rotation_counter(tmp_path):
    from bankrag.chat import MAX_TURNS_PER_CONVERSATION

    s = _settings(tmp_path)
    skills = load_skills(ROOT / "skills")
    cs = ChatSession(s, skills, project=FakeProject())
    cs.history = [{"role": "user", "content": "q1"}, {"role": "assistant", "content": "a1"}, {"role": "user", "content": "q2"}]
    items = cs._recap_items()
    assert items[0]["role"] == "developer" and "q1 | q2" in items[0]["content"] and "a1" in items[0]["content"]
    rec = new_record(); rec.conversation_id = "conv_a"
    for i in range(8):
        ans = Answer(skill_id="credit-card", confidence=0.9, text=f"a{i}", trace={"conversation": {"id": "conv_a" if i >= 5 else "conv_old"}})
        append_turns(rec, f"q{i}", ans)
    restored = ChatSession.from_record(s, skills, rec, project=FakeProject())
    assert restored.turns_in_conversation == 3 and MAX_TURNS_PER_CONVERSATION == 6


def test_backup_and_restore(tmp_path, monkeypatch):
    from bankrag.sessions import backup_db, restore_db

    s = _settings(tmp_path)
    monkeypatch.setenv("SQLITE_DB_PATH", str(tmp_path / "local" / "bankrag.db"))
    rec = new_record(); append_turns(rec, "q", Answer(skill_id="general", confidence=0.5, text="a")); save_session(s, rec)
    dest = tmp_path / "share" / "bankrag.db.bak"
    assert backup_db(s, dest) and dest.exists()
    monkeypatch.setenv("SQLITE_DB_PATH", str(tmp_path / "fresh" / "bankrag.db"))
    assert restore_db(s, dest) and load_session(s, rec.id).title == "q"
    assert restore_db(s, dest) is False  # already present
