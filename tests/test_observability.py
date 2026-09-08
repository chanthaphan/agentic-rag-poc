from bankrag import observability as OBS
from bankrag.config import Settings

SPANS = [
    {"timestamp": "t1", "name": "invoke_agent bank-concierge:1", "operation_Id": "op1", "operation_ParentId": "x", "id": "s1", "duration": 8152.0, "agent": "bank-concierge", "op": "invoke_agent", "model": "gpt-4.1-mini", "rid": "resp_c", "inp": None, "outp": None, "cached": None, "tool": "", "conv": ""},
    {"timestamp": "t2", "name": "execute_tool remote_a2a", "operation_Id": "op1", "operation_ParentId": "s1", "id": "s2", "duration": 5000.0, "agent": "bank-concierge", "op": "execute_tool", "model": "", "rid": "resp_c", "inp": None, "outp": None, "cached": None, "tool": "remote_a2a_a2a-debit-card.SendMessage", "conv": ""},
    {"timestamp": "t3", "name": "invoke_agent bank-debit-card:11", "operation_Id": "op1", "operation_ParentId": "s2", "id": "s3", "duration": 3389.0, "agent": "bank-debit-card", "op": "invoke_agent", "model": "gpt-4.1-mini", "rid": "resp_s", "inp": None, "outp": None, "cached": None, "tool": "", "conv": ""},
    {"timestamp": "t4", "name": "execute_tool mcp", "operation_Id": "op1", "operation_ParentId": "s3", "id": "s4", "duration": 1500.0, "agent": "bank-debit-card", "op": "execute_tool", "model": "", "rid": "resp_s", "inp": None, "outp": None, "cached": None, "tool": "mcp_knowledge-base.knowledge_base_retrieve", "conv": ""},
    {"timestamp": "t5", "name": "chat gpt-4.1-mini", "operation_Id": "op1", "operation_ParentId": "s3", "id": "s5", "duration": 1800.0, "agent": "bank-debit-card", "op": "chat", "model": "gpt-4.1-mini-2025-04-14", "rid": "resp_s", "inp": 18022, "outp": 105, "cached": 0, "tool": "", "conv": ""},
    {"timestamp": "t6", "name": "chat gpt-4.1-mini", "operation_Id": "op1", "operation_ParentId": "s1", "id": "s6", "duration": 800.0, "agent": "bank-concierge", "op": "chat", "model": "gpt-4.1-mini-2025-04-14", "rid": "resp_c", "inp": 3462, "outp": 82, "cached": 3326, "tool": "", "conv": ""},
]


def test_summarize_per_agent():
    s = OBS.summarize(SPANS)
    assert s["bank-debit-card"]["input_tokens"] == 18022 and s["bank-debit-card"]["output_tokens"] == 105 and s["bank-debit-card"]["chat_calls"] == 1
    assert s["bank-debit-card"]["tools"] == ["mcp_knowledge-base.knowledge_base_retrieve"] and s["bank-debit-card"]["ms"] == 3389.0
    assert s["bank-concierge"]["cached_tokens"] == 3326 and s["bank-concierge"]["response_ids"] == ["resp_c"]


def test_reconcile_trace_adds_specialist_usage(monkeypatch):
    settings = Settings.load()
    settings.appinsights_app_id = "app"
    monkeypatch.setattr(OBS, "spans_for_response", lambda s, rid, timespan="PT6H", question="": SPANS if rid == "resp_c" else [])
    trace = {"response_id": "resp_c", "usage": {"agent": {"input_tokens": 3462, "output_tokens": 141, "total_tokens": 3603, "cached_tokens": 3326, "reasoning_tokens": 0}, "total": {"input_tokens": 3462, "output_tokens": 141, "total_tokens": 3603, "cached_tokens": 3326, "reasoning_tokens": 0}},
             "retrieval": {"calls": 0}, "cost": {"agent": {"total_usd": 0.0015}, "router": {"total_usd": 0.0}, "retrieval_usd": 0.0, "total_usd": 0.0015},
             "handoff": {"mode": "a2a", "concierge": "bank-concierge", "specialist": "debit-card", "usage_pending": True}, "timings_ms": {"agent": 9000, "total": 9100}}
    pricing = {"models": {"gpt-4.1-mini": {"input": 0.4, "cached_input": 0.1, "output": 1.6}}, "retrieval_per_call": 0.002, "currency": "USD"}
    assert OBS.reconcile_trace(settings, trace, pricing) is True
    assert trace["usage"]["specialist"]["input_tokens"] == 18022 and trace["usage"]["total"]["input_tokens"] == 3462 + 18022
    assert trace["retrieval"]["calls"] == 1 and trace["cost"]["specialist"]["agents"][0]["agent"] == "bank-debit-card"
    assert trace["cost"]["total_usd"] > 0.0015 + 0.002 and trace["handoff"]["usage_pending"] is False and trace["timings_ms"]["specialist"] == 3389
    # no spans yet: attempt counter grows, still pending
    trace2 = {"response_id": "resp_missing", "usage": {"agent": {}}, "handoff": {"concierge": "bank-concierge", "usage_pending": True}}
    assert OBS.reconcile_trace(settings, trace2, pricing) is False and trace2["handoff"]["reconcile_attempts"] == 1 and trace2["handoff"]["usage_pending"] is True


def test_spans_for_response_falls_back_to_time_window(monkeypatch):
    settings = Settings.load(); settings.appinsights_app_id = "app"
    concierge = [{"timestamp": "2026-09-09T17:21:07.337Z", "name": "invoke_agent bank-concierge:1", "operation_Id": "opA", "operation_ParentId": "", "id": "1", "duration": 6000.0, "agent": "bank-concierge", "op": "invoke_agent", "model": "", "rid": "resp_c", "inp": None, "outp": None, "cached": None, "tool": "", "conv": ""},
                 {"timestamp": "2026-09-09T17:21:07.337Z", "name": "execute_tool", "operation_Id": "opA", "operation_ParentId": "1", "id": "2", "duration": 5000.0, "agent": "bank-concierge", "op": "execute_tool", "model": "", "rid": "resp_c", "inp": None, "outp": None, "cached": None, "tool": "remote_a2a_a2a-credit-card.SendMessage", "conv": ""}]
    other = [{"timestamp": "2026-09-09T17:21:10.0Z", "name": "invoke_agent bank-credit-card:12", "operation_Id": "opB", "operation_ParentId": "", "id": "3", "duration": 3000.0, "agent": "bank-credit-card", "op": "invoke_agent", "model": "", "rid": "resp_s", "inp": None, "outp": None, "cached": None, "tool": "", "conv": "", "inmsg": "annual fee of Infinite?"},
             {"timestamp": "2026-09-09T17:21:11.9Z", "name": "chat", "operation_Id": "opB", "operation_ParentId": "3", "id": "4", "duration": 1500.0, "agent": "bank-credit-card", "op": "chat", "model": "gpt-4.1-mini", "rid": "resp_s", "inp": 18184, "outp": 120, "cached": 0, "tool": "", "conv": "", "inmsg": "annual fee of Infinite?"},
             {"timestamp": "2026-09-09T17:21:10.5Z", "name": "invoke_agent bank-credit-card:12", "operation_Id": "opC", "operation_ParentId": "", "id": "5", "duration": 3000.0, "agent": "bank-credit-card", "op": "invoke_agent", "model": "", "rid": "resp_x", "inp": None, "outp": None, "cached": None, "tool": "", "conv": "", "inmsg": "lounge visits?"},
             {"timestamp": "2026-09-09T17:21:12.0Z", "name": "chat", "operation_Id": "opC", "operation_ParentId": "5", "id": "6", "duration": 1500.0, "agent": "bank-credit-card", "op": "chat", "model": "gpt-4.1-mini", "rid": "resp_x", "inp": 9000, "outp": 50, "cached": 0, "tool": "", "conv": "", "inmsg": "lounge visits?"}]
    calls = {"n": 0}
    def fake_query(s, kql, timespan="PT6H"):
        calls["n"] += 1
        return concierge if "gen_ai.response.id'] ==" in kql else other
    monkeypatch.setattr(OBS, "query", fake_query)
    spans = OBS.spans_for_response(settings, "resp_c", question="annual fee of Infinite?")
    ops = {s["operation_Id"] for s in spans}
    assert ops == {"opA", "opB"} and calls["n"] == 2
    assert OBS.summarize(spans)["bank-credit-card"]["input_tokens"] == 18184
