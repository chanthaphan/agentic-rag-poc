"""The persona is configuration, not code: no file should carry a name of its own."""
import re
from pathlib import Path

from bankrag.config import Settings, _gender
from bankrag.skills import persona_words

ROOT = Path(__file__).resolve().parents[1]
# every persona this POC has worn; none of them may appear in code, markup or deploy scripts
NAMES = ("ต้า", "Tah", "เกรส", "Grace", "เคอร์วอน", "Kervon")
SEARCH = ("src/bankrag", "web", "infra", "skills")


def test_no_file_carries_the_personas_name():
    """A name baked into a greeting, a prompt or a deploy script is one the Settings screen cannot change."""
    offenders = []
    for folder in SEARCH:
        for p in sorted((ROOT / folder).rglob("*")):
            if not p.is_file() or p.suffix.lower() not in (".py", ".js", ".html", ".css", ".sh", ".md", ".yaml"):
                continue
            if "_crawl" in p.parts or "_uploads" in p.parts:  # crawled bank content, not ours
                continue
            text = p.read_text(encoding="utf-8", errors="ignore")
            for name in NAMES:
                if re.search(re.escape(name), text):
                    offenders.append(f"{p.relative_to(ROOT)}: {name}")
    assert not offenders, "the persona's name belongs in .env / Studio, not in: " + "; ".join(offenders)


def test_an_unconfigured_assistant_has_a_neutral_name(monkeypatch, tmp_path):
    """With nothing configured the assistant is simply 'the assistant', not last week's persona."""
    for key in ("ASSISTANT_NAME", "ASSISTANT_NAME_EN", "ASSISTANT_GENDER"):
        monkeypatch.delenv(key, raising=False)
    s = Settings.load(tmp_path)  # a root with no .env and no overlay
    assert s.assistant_name == "ผู้ช่วย" and s.assistant_name_en == "Assistant"
    assert s.assistant_gender == "male"


def test_the_gender_setting_is_normalised():
    assert _gender("female") == "female" and _gender("F") == "female" and _gender("หญิง") == "female"
    assert _gender("male") == "male" and _gender("") == "male" and _gender("anything else") == "male"
    assert persona_words("female")["particle"] == "ค่ะ" and persona_words("male")["particle"] == "ครับ"
    assert persona_words("nonsense")["pronoun_th"] == "ผม"


def test_the_off_topic_reply_speaks_in_the_personas_voice(tmp_path):
    from bankrag.chat import offtopic_reply

    s = Settings.load(tmp_path)
    s.assistant_gender = "male"
    assert offtopic_reply(s, "th").startswith("ขออภัยครับ")
    s.assistant_gender = "female"
    assert offtopic_reply(s, "th").startswith("ขออภัยค่ะ")
    assert "{particle}" not in offtopic_reply(s, "en")
