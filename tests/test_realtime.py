"""Speech mode: the session the browser is given, the key that opens it, the tools behind it and the call it leaves.

Nothing here touches Azure: the mint goes through an httpx MockTransport and the retrieval is a stub, so the shape of
what we send and store is checked offline."""
import base64
import json
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from bankrag import api, realtime as RT, sessions as SESS
from bankrag.config import Settings
from bankrag.models import Reference
from bankrag.skills import load_base, load_skills

ROOT = Path(__file__).resolve().parents[1]


def _settings(**over) -> Settings:
    s = Settings.load(ROOT)
    s.aoai_endpoint = "https://acct.openai.azure.com"
    s.aoai_api_key = "k"
    # a binding on the developer's own machine (.env or the Studio overlay) must not decide what these tests see
    s.realtime_endpoint = s.realtime_api_key = s.llm_api_key = s.llm_base_url = ""
    s.llm_provider = "azure"
    s.realtime_deployment = "gpt-realtime-2.1"
    s.realtime_voice = "cedar"
    for k, v in over.items():
        setattr(s, k, v)
    return s


def _hdr(email, name="Some One"):
    claims = [{"typ": "name", "val": name}, {"typ": "preferred_username", "val": email}]
    return {"x-ms-client-principal": base64.b64encode(json.dumps({"claims": claims}).encode()).decode()}


def test_the_session_carries_the_persona_the_voice_and_the_tools():
    s = _settings(assistant_name="ต้า", assistant_name_en="Tah", assistant_gender="male")
    skills, base = load_skills(ROOT / "skills"), load_base(ROOT / "skills")
    cfg = RT.session_config(s, skills, base, lang="th")

    assert cfg["type"] == "realtime" and cfg["model"] == "gpt-realtime-2.1"
    assert cfg["audio"]["output"]["voice"] == "cedar"
    assert cfg["audio"]["input"]["turn_detection"]["type"] == "semantic_vad"
    assert cfg["audio"]["input"]["transcription"]["language"] == "th"
    assert "language" not in RT.session_config(s, skills, base)["audio"]["input"]["transcription"]

    names = [t["name"] for t in cfg["tools"]]
    assert names == [RT.SEARCH_TOOL, "fx_rate", "find_branch"]
    topics = cfg["tools"][0]["parameters"]["properties"]["topic"]["enum"]
    assert "credit-card" in topics and "general" in topics
    assert "bank-services" not in topics  # it answers from the live tools, not from documents

    text = cfg["instructions"]
    assert "Tah" in text and "ครับ" in text and "You are a man" in text
    assert "{" not in text.replace("{skill}", "") or "{assistant_name" not in text
    assert "Speech mode" in text and "never read out a URL" in text
    assert "ChatGPT" in text  # the model is told not to call itself that


def test_the_persona_gender_changes_the_spoken_voice():
    skills, base = load_skills(ROOT / "skills"), load_base(ROOT / "skills")
    female = RT.speech_instructions(_settings(assistant_gender="female"), base)
    assert "You are a woman" in female and "ค่ะ" in female and "{particle" not in female
    assert RT.tool_schemas(_settings(), skills)[0]["name"] == RT.SEARCH_TOOL


def _mint_with(handler, settings):
    skills, base = load_skills(ROOT / "skills"), load_base(ROOT / "skills")
    cfg = RT.session_config(settings, skills, base)
    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        return RT.mint_client_secret(settings, cfg, client=c)


def test_the_key_is_minted_with_the_account_key():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("api-key")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"value": "ek_123", "expires_at": 1789})

    out = _mint_with(handler, _settings())
    assert out["value"] == "ek_123"
    assert seen["url"] == "https://acct.openai.azure.com/openai/v1/realtime/client_secrets"
    assert seen["auth"] == "k"
    assert seen["body"]["session"]["model"] == "gpt-realtime-2.1"
    assert seen["body"]["expires_after"]["seconds"] == RT.SECRET_TTL_S


def test_without_a_key_the_signed_in_identity_mints_it(monkeypatch):
    monkeypatch.setattr(RT, "token", lambda scope: "tok")
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"value": "ek_9"})

    assert _mint_with(handler, _settings(aoai_api_key=""))["value"] == "ek_9"
    assert seen["auth"] == "Bearer tok"


def test_a_refusal_is_reported_not_swallowed():
    def handler(request):
        return httpx.Response(403, text="deployment not found")

    try:
        _mint_with(handler, _settings())
    except RT.RealtimeError as e:
        assert "403" in str(e) and "deployment not found" in str(e)
    else:
        raise AssertionError("a refused mint must raise")

    for bad in (_settings(realtime_deployment=""), _settings(aoai_endpoint="")):
        try:
            RT.mint_client_secret(bad, {})
        except RT.RealtimeError:
            pass
        else:
            raise AssertionError("an unconfigured speech mode must raise")


def test_the_session_route_hands_the_browser_a_key_and_nothing_else(monkeypatch):
    monkeypatch.setattr(api.settings, "realtime_deployment", "gpt-realtime-2.1")
    monkeypatch.setattr(api.settings, "realtime_voice", "cedar")
    monkeypatch.setattr(api.settings, "aoai_endpoint", "https://acct.openai.azure.com")
    monkeypatch.setattr(RT, "mint_client_secret", lambda s, cfg, **kw: {"value": "ek_42", "expires_at": 99})
    c = TestClient(api.app)
    body = c.post("/realtime/session", json={"lang": "th"}).json()
    assert body["client_secret"] == "ek_42" and body["model"] == "gpt-realtime-2.1" and body["voice"] == "cedar"
    assert body["calls_url"].endswith("/openai/v1/realtime/calls")
    assert "instructions" not in body  # the prompt stays on the server

    def boom(s, cfg, **kw):
        raise RT.RealtimeError("401 nope")

    monkeypatch.setattr(RT, "mint_client_secret", boom)
    r = c.post("/realtime/session", json={})
    assert r.status_code == 502 and "401" in r.json()["detail"]


def _stub_retrieval(monkeypatch):
    calls = {}
    refs = [Reference(id="1", title="Platinum", source_url="https://x/platinum", content="ค่าธรรมเนียมรายปี 3,000 บาท")]

    def rest_retrieve(settings, kb_name, query, *, ks_name="", max_docs=None, variants=None):
        calls.update(kb_name=kb_name, query=query, ks_name=ks_name, max_docs=max_docs)
        return "", refs

    monkeypatch.setattr(RT.KBT, "rest_retrieve", rest_retrieve)
    return calls


def test_the_search_tool_reads_the_same_knowledge_base_the_text_agent_does(monkeypatch):
    calls = _stub_retrieval(monkeypatch)
    c = TestClient(api.app)
    out = c.post("/realtime/tool", json={"name": "search_bank_knowledge", "arguments": {"query": "ค่าธรรมเนียม", "topic": "credit-card"}}).json()
    assert out["skill_id"] == "credit-card" and "Platinum" in out["output"] and "3,000" in out["output"]
    assert out["references"][0]["title"] == "Platinum" and "content" not in out["references"][0]
    assert calls["kb_name"].endswith("credit-card") or calls["kb_name"].startswith("kb-")
    assert calls["query"] == "ค่าธรรมเนียม"

    # a topic the model made up still answers, from the general base
    assert c.post("/realtime/tool", json={"name": "search_bank_knowledge", "arguments": {"query": "x", "topic": "nope"}}).json()["skill_id"] == "general"
    # arguments may arrive as the JSON string the model produced
    assert c.post("/realtime/tool", json={"name": "search_bank_knowledge", "arguments": '{"query": "x", "topic": "general"}'}).status_code == 200
    assert c.post("/realtime/tool", json={"name": "search_bank_knowledge", "arguments": "{oops"}).status_code == 400
    assert c.post("/realtime/tool", json={"name": "search_bank_knowledge", "arguments": {"topic": "general"}}).status_code == 400
    assert c.post("/realtime/tool", json={"name": "rm_rf", "arguments": {}}).status_code == 400


def test_the_live_service_tools_are_the_same_ones_the_agents_call(monkeypatch):
    seen = {}

    def fx(settings, currency):
        seen["currency"] = currency
        return {"found": True, "currency": "JPY", "buying": 0.2059}

    def branch(settings, lat=None, lon=None, province="", kind="branch", limit=5, name=""):
        seen.update(lat=lat, lon=lon, kind=kind, province=province, limit=limit, name=name)
        return {"found": True, "branches": [{"name": "Silom"}]}

    monkeypatch.setattr(RT, "fx_rate_impl", fx)
    monkeypatch.setattr(RT, "find_branch_impl", branch)
    c = TestClient(api.app)
    out = c.post("/realtime/tool", json={"name": "fx_rate", "arguments": {"currency": "เยน"}}).json()
    assert seen["currency"] == "เยน" and json.loads(out["output"])["currency"] == "JPY" and out["skill_id"] == "bank-services"

    out = c.post("/realtime/tool", json={"name": "find_branch", "arguments": {"kind": "exchange"}, "lat": 13.7, "lon": 100.5}).json()
    assert (seen["lat"], seen["lon"], seen["kind"]) == (13.7, 100.5, "exchange")
    assert json.loads(out["output"])["found"] is True and out["places"] == [{"name": "Silom"}]


def test_a_call_is_saved_as_a_conversation_its_owner_can_read(tmp_path, monkeypatch):
    monkeypatch.setenv("SQLITE_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setattr(api.settings, "state_dir", tmp_path / ".state")
    SESS._schema_done.clear()
    c = TestClient(api.app)
    pim = _hdr("pim@bangkokbank.com", "Pim W")

    turn = {"question": "ค่าธรรมเนียมบัตรเท่าไหร่", "answer": "สามพันบาทต่อปีครับ", "language": "th", "skill_id": "credit-card",
            "tool_calls": [{"type": "function", "name": "search_bank_knowledge"}],
            "references": [{"title": "Platinum", "source_url": "https://x", "content": "dropped"}],
            "usage": {"input_tokens": 900, "output_tokens": 120}}
    body = c.post("/realtime/turns", json={"turns": [turn], "model": "gpt-realtime-2.1"}, headers=pim).json()
    sid = body["session_id"]
    assert SESS.ID_RE.match(sid) and body["turns"] == 2 and body["title"].startswith("ค่าธรรมเนียม")

    rec = c.get(f"/sessions/{sid}", headers=pim).json()
    assert rec["source"] == "voice"
    answer = rec["turns"][1]
    assert answer["skill_id"] == "credit-card" and answer["text"].endswith("ครับ") and answer["language"] == "th"
    assert answer["trace"]["mode"] == "realtime" and answer["trace"]["model"] == "gpt-realtime-2.1"
    assert answer["trace"]["usage"]["total"]["input_tokens"] == 900
    assert answer["references"][0]["title"] == "Platinum" and answer["tool_calls"][0]["name"] == "search_bank_knowledge"
    assert rec["turns"][0]["by"] == "Pim W"

    # a second turn continues the same call
    assert c.post("/realtime/turns", json={"session_id": sid, "turns": [turn]}, headers=pim).json()["turns"] == 4
    assert c.get(f"/sessions/{sid}", headers=pim).json()["source"] == "voice"
    assert [r["id"] for r in c.get("/sessions", headers=pim).json()] == [sid]
    # somebody else's call is not theirs to read or extend
    other = _hdr("kanit@bangkokbank.com", "Kanit")
    assert c.get(f"/sessions/{sid}", headers=other).status_code == 403
    assert c.post("/realtime/turns", json={"session_id": sid, "turns": [turn]}, headers=other).status_code == 403
    # bad input is refused rather than stored
    assert c.post("/realtime/turns", json={"session_id": "zz", "turns": [turn]}, headers=pim).status_code == 400
    assert c.post("/realtime/turns", json={"turns": []}, headers=pim).status_code == 400
    # Studio can filter the call out of the conversation list by source
    rows = SESS.question_rows(api.settings, source="voice")
    assert rows and rows[0]["source"] == "voice"


def test_a_spoken_turn_is_costed_with_its_audio_tokens(tmp_path, monkeypatch):
    """Voice turns used to save tokens but no cost, so Studio's totals read zero for every call."""
    from bankrag.pricing import split_usage, voice_turn_cost

    pricing = {"currency": "USD", "models": {
        "gpt-realtime-2.1": {"input": 4.0, "cached_input": 0.4, "output": 16.0,
                             "audio_input": 32.0, "cached_audio_input": 0.4, "audio_output": 64.0},
        "default": {"input": 1.0, "cached_input": 0.25, "output": 4.0}}}
    usage = {"total_tokens": 3000, "input_tokens": 2000, "output_tokens": 1000,
             "input_token_details": {"text_tokens": 1200, "audio_tokens": 800, "cached_tokens": 500,
                                     "cached_tokens_details": {"text_tokens": 400, "audio_tokens": 100}},
             "output_token_details": {"text_tokens": 200, "audio_tokens": 800}}
    u = split_usage(usage)
    assert (u["text_in"], u["cached_text_in"], u["audio_in"], u["cached_audio_in"]) == (800, 400, 700, 100)
    assert (u["text_out"], u["audio_out"]) == (200, 800)
    cost = voice_turn_cost(pricing, "gpt-realtime-2.1", usage)
    # the audio side dominates: text alone would be less than a tenth of this
    assert round(cost["total_usd"], 6) == 0.0802
    assert cost["agent"]["audio_output_usd"] > cost["agent"]["output_usd"] * 10
    # a chat-shaped usage record (no details) still costs as plain text
    assert round(voice_turn_cost(pricing, "gpt-4.1-mini", {"input_tokens": 1000, "output_tokens": 100})["total_usd"], 6) == 0.0014

    # and the endpoint stores it, so the turn rows Studio sums carry a cost
    monkeypatch.setenv("SQLITE_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setattr(api.settings, "state_dir", tmp_path / ".state")
    monkeypatch.setattr(api.settings, "realtime_deployment", "gpt-realtime-2.1")
    SESS._schema_done.clear()
    c = TestClient(api.app)
    body = c.post("/realtime/turns", json={"turns": [{"question": "q", "answer": "a", "usage": usage}]}).json()
    rec = c.get(f"/sessions/{body['session_id']}").json()
    trace = rec["turns"][1]["trace"]
    assert trace["cost"]["total_usd"] > 0 and trace["cost"]["agent"]["audio_output_usd"] > 0
    rows = SESS.question_rows(api.settings, source="voice")
    assert rows and rows[0]["cost_usd"] and rows[0]["cost_usd"] > 0
    assert SESS.stats(api.settings)["cost_usd"] > 0


def test_an_answer_is_prepared_for_a_voice_before_it_is_read():
    """What is spoken is the answer as a person reads it, not the markup a screen shows."""
    t = RT.speakable("**ค่าธรรมเนียม** 3,000 บาท [ดูที่นี่](https://x/a)\n- ข้อ 1\n- ข้อ 2\n\n| a | b |\nครับ")
    assert "**" not in t and "https://" not in t and "|" not in t
    assert "ดูที่นี่" in t and "ข้อ 1" in t and t.endswith("ครับ")
    # a very long answer is cut at a sentence end rather than refused
    long = ("ประโยคหนึ่ง. " * 900)
    assert len(RT.speakable(long)) <= RT.MAX_SPOKEN_CHARS and RT.speakable(long).endswith(".")


def test_the_play_endpoint_returns_audio_in_the_personas_voice(monkeypatch):
    """Playback goes through an audio model of the realtime family, so a played answer and a call are one voice."""
    import base64

    seen = {}

    def audio_handler(request):
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        payload = {"choices": [{"message": {"audio": {"data": base64.b64encode(b"ID3fake-mp3").decode()}}}]}
        return httpx.Response(200, json=payload)

    s = _settings(tts_deployment="gpt-audio-1.5", realtime_voice="cedar")
    with httpx.Client(transport=httpx.MockTransport(audio_handler)) as client:
        audio = RT.speak(s, "สวัสดีครับ", client=client)
    assert audio.startswith(b"ID3")
    assert seen["url"] == "https://acct.openai.azure.com/openai/v1/chat/completions"
    assert seen["body"]["audio"] == {"voice": "cedar", "format": "mp3"} and seen["body"]["modalities"] == ["text", "audio"]
    assert seen["body"]["messages"][-1]["content"] == "สวัสดีครับ"  # read out as written, nothing added

    # a plain text-to-speech deployment is still accepted, on its own endpoint
    def tts_handler(request):
        seen["tts_url"] = str(request.url)
        seen["tts_body"] = json.loads(request.content)
        return httpx.Response(200, content=b"ID3tts")

    with httpx.Client(transport=httpx.MockTransport(tts_handler)) as client:
        assert RT.speak(_settings(tts_deployment="gpt-4o-mini-tts"), "hi", client=client) == b"ID3tts"
    assert seen["tts_url"].endswith("/openai/v1/audio/speech") and seen["tts_body"]["response_format"] == "mp3"

    # through the route, with the page's own fallback signalled by a 503 when nothing is configured
    monkeypatch.setattr(api.settings, "tts_deployment", "gpt-audio-1.5")
    monkeypatch.setattr(api.settings, "aoai_endpoint", "https://acct.openai.azure.com")
    real_speak = RT.speak
    monkeypatch.setattr(RT, "speak", lambda settings, text, **kw: b"ID3ok")
    c = TestClient(api.app)
    r = c.post("/tts", json={"text": "**สวัสดี** ครับ"})
    assert r.status_code == 200 and r.headers["content-type"] == "audio/mpeg" and r.content == b"ID3ok"
    assert c.post("/tts", json={"text": "   "}).status_code == 400
    monkeypatch.setattr(api.settings, "tts_deployment", "")
    monkeypatch.setattr(RT, "speak", real_speak)
    assert c.post("/tts", json={"text": "hello"}).status_code == 503  # the page falls back to the browser voice
    assert c.get("/app/config").json()["tts_enabled"] is False


def test_the_call_can_live_on_its_own_azure_resource(monkeypatch):
    """A realtime resource is often handed out on its own, carrying nothing but the realtime model.

    Binding one must move the CALL and nothing else: reading an answer aloud keeps using the app's own account,
    which is where the audio model actually is."""
    s = _settings(realtime_endpoint="https://realtime-only.openai.azure.com", realtime_api_key="rt-key")
    assert s.speech_endpoint == "https://realtime-only.openai.azure.com"
    assert s.realtime_client_secrets_url == "https://realtime-only.openai.azure.com/openai/v1/realtime/client_secrets"
    assert s.realtime_calls_url.endswith("/openai/v1/realtime/calls")
    assert RT.auth_headers(s) == {"api-key": "rt-key"}        # the call goes to the bound resource
    # the play button stays on the account that carries the audio model
    assert s.speech_url == "https://acct.openai.azure.com/openai/v1/audio/speech"
    assert RT.audio_headers(s) == {"api-key": "k"}

    seen = {}

    def handler(request):
        seen["url"], seen["key"] = str(request.url), request.headers.get("api-key")
        return httpx.Response(200, json={"value": "ek_rt"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        assert RT.mint_client_secret(s, {"type": "realtime", "model": "gpt-realtime-2.1"}, client=c)["value"] == "ek_rt"
    assert seen["url"].startswith("https://realtime-only.openai.azure.com") and seen["key"] == "rt-key"

    # unbound, everything stays on the one account
    plain = _settings()
    assert plain.speech_endpoint == "https://acct.openai.azure.com"
    assert RT.auth_headers(plain) == RT.audio_headers(plain) == {"api-key": "k"}
