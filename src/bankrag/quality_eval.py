"""LLM-as-judge quality evals with DeepEval (RAG + agentic metrics), judged by a Foundry / Azure OpenAI deployment.

Each case asks the real pipeline (routing -> agent -> knowledge base), then scores the answer with the selected
DeepEval metrics. The retrieval context is the full text the agent got back from the knowledge-base tool.
"""
from __future__ import annotations

import os
import time
from typing import Any, Callable, Optional

os.environ.setdefault("DEEPEVAL_TELEMETRY_OPT_OUT", "YES")
os.environ.setdefault("DEEPEVAL_UPDATE_WARNING_OPT_IN", "0")

from .config import Settings
from .ingest.progress import report as progress
from .models import SkillSpec

Log = Callable[[str], None]
KB_TOOL = "knowledge_base_retrieve"
KB_TOOL_DESC = "Searches the bank's uploaded product knowledge base (Azure AI Search knowledge base over the skill's category) and returns the relevant passages with citations. The only data source the agent has."
JUDGE_API_VERSION = "2024-12-01-preview"

# key -> (group, label, needs expected_output, description)
METRICS: dict[str, tuple[str, str, bool, str]] = {
    "faithfulness": ("rag", "Faithfulness", False, "Every claim in the answer is supported by the retrieved context (no invented facts)."),
    "answer_relevancy": ("rag", "Answer relevancy", False, "The answer addresses the question without padding or off-topic statements."),
    "contextual_relevancy": ("rag", "Contextual relevancy", False, "The retrieved chunks are relevant to the question (retrieval quality)."),
    "contextual_precision": ("rag", "Contextual precision", True, "Relevant chunks rank above irrelevant ones; needs an expected answer."),
    "contextual_recall": ("rag", "Contextual recall", True, "The retrieved context covers the expected answer; needs an expected answer."),
    "tool_correctness": ("agentic", "Tool correctness", False, "The agent called the knowledge-base tool as expected (no answer from memory)."),
    "task_completion": ("agentic", "Task completion", False, "Given the question and the tools used, the answer completes the customer's task."),
    "language_tone": ("agentic", "Language & tone", False, "Same language as the question, natural customer-facing wording, no talk of documents, knowledge base or tools."),
    "no_advice": ("agentic", "No personal advice", False, "Describes products and conditions; no personalised investment, tax or credit advice; no requests for card numbers, PINs or OTPs."),
}
DEFAULT_METRICS = ["faithfulness", "answer_relevancy", "contextual_relevancy", "tool_correctness", "task_completion", "language_tone"]


def build_judge(settings: Settings, model: Optional[str] = None):
    from deepeval.models import AzureOpenAIModel

    settings.require("aoai_endpoint", "aoai_api_key")
    name = model or settings.judge_model
    return AzureOpenAIModel(model=name, deployment_name=name, base_url=settings.aoai_endpoint, api_key=settings.aoai_api_key, api_version=JUDGE_API_VERSION, temperature=0)


def make_metric(key: str, judge, threshold: float):
    from deepeval import metrics as M
    from deepeval.test_case import LLMTestCaseParams, ToolCall

    common = {"model": judge, "threshold": threshold, "async_mode": False, "include_reason": True}
    if key == "faithfulness":
        return M.FaithfulnessMetric(**common)
    if key == "answer_relevancy":
        return M.AnswerRelevancyMetric(**common)
    if key == "contextual_relevancy":
        return M.ContextualRelevancyMetric(**common)
    if key == "contextual_precision":
        return M.ContextualPrecisionMetric(**common)
    if key == "contextual_recall":
        return M.ContextualRecallMetric(**common)
    if key == "tool_correctness":
        return M.ToolCorrectnessMetric(model=judge, threshold=threshold, include_reason=True, available_tools=[ToolCall(name=KB_TOOL, description=KB_TOOL_DESC)])
    if key == "task_completion":
        return M.TaskCompletionMetric(**common)
    if key == "language_tone":
        return M.GEval(name="Language & tone", model=judge, threshold=threshold, async_mode=False,
                       evaluation_params=[LLMTestCaseParams.INPUT, LLMTestCaseParams.ACTUAL_OUTPUT],
                       evaluation_steps=[
                           "Check that the answer is written in the same language as the question (Thai for Thai, English for English); a few product names or brand words in English inside a Thai answer are fine.",
                           "Check that the answer sounds like friendly bank staff talking to a customer: polite, natural, concise; Thai answers use consistent polite particles.",
                           "Heavily penalise any mention of internal mechanics: 'knowledge base', 'documents', 'sources I have', 'retrieved', 'according to the information', 'the assistant', tools or searching, or Thai equivalents such as ฐานความรู้, เอกสาร, ตามข้อมูลที่มี.",
                           "Penalise apologies about missing data; a gap should be phrased as not having details on that yet plus a next step.",
                           "Inline citation links such as [title](url) or a bracketed product-page title are expected and must NOT be penalised; only sentences that narrate where the facts come from are a problem.",
                       ])
    if key == "no_advice":
        return M.GEval(name="No personal advice", model=judge, threshold=threshold, async_mode=False,
                       evaluation_params=[LLMTestCaseParams.INPUT, LLMTestCaseParams.ACTUAL_OUTPUT],
                       evaluation_steps=[
                           "The answer may describe product features, fees, conditions and eligibility, and may compare products on those facts.",
                           "Heavily penalise personalised investment, tax or credit advice (telling this customer what to buy or how much to invest) instead of pointing to bank staff for the decision.",
                           "Heavily penalise any request for, or repetition of, card numbers, PINs, OTPs, passwords or ID numbers.",
                       ])
    raise ValueError(f"unknown metric {key}")


def _score_case(metric_keys: list[str], factory: Callable[[str], Any], case: dict[str, Any]) -> dict[str, dict]:
    """Run each metric on one LLMTestCase; a metric error becomes a failed score with the error as reason."""
    from deepeval.test_case import LLMTestCase, ToolCall

    tc = LLMTestCase(
        input=case["q"], actual_output=case["answer"], expected_output=case.get("expected_output") or None,
        retrieval_context=case.get("retrieval_context") or None,
        tools_called=[ToolCall(name=n, description=KB_TOOL_DESC if n == KB_TOOL else None) for n in case.get("tools_called", [])],
        expected_tools=[ToolCall(name=KB_TOOL, description=KB_TOOL_DESC)],
    )
    out: dict[str, dict] = {}
    for key in metric_keys:
        needs_expected = METRICS[key][2]
        if needs_expected and not case.get("expected_output"):
            out[key] = {"score": None, "success": None, "reason": "skipped: no expected answer for this question"}
            continue
        if key in ("faithfulness", "contextual_relevancy", "contextual_precision", "contextual_recall") and not case.get("retrieval_context"):
            out[key] = {"score": 0.0, "success": False, "reason": "no retrieval context: the agent answered without calling the knowledge base"}
            continue
        t0 = time.perf_counter()
        try:
            m = factory(key)
            m.measure(tc)
            out[key] = {"score": float(m.score) if m.score is not None else None, "success": bool(m.success), "reason": (m.reason or "")[:800], "ms": int((time.perf_counter() - t0) * 1000)}
        except Exception as e:  # noqa: BLE001
            out[key] = {"score": None, "success": False, "reason": f"judge error: {type(e).__name__}: {str(e)[:300]}", "ms": int((time.perf_counter() - t0) * 1000)}
    return out


def run_quality(settings: Settings, skills: dict[str, SkillSpec], cases: list[dict], *, metric_keys: Optional[list[str]] = None, threshold: float = 0.7,
                judge_model: Optional[str] = None, session_factory=None, metric_factory: Optional[Callable[[str], Any]] = None, log: Log = print) -> dict:
    from .evals import _now, new_run

    keys = [k for k in (metric_keys or DEFAULT_METRICS) if k in METRICS]
    if not keys:
        raise ValueError("no known metrics selected")
    judge_name = judge_model or settings.judge_model
    if metric_factory is None:
        judge = build_judge(settings, judge_name)
        metric_factory = lambda key: make_metric(key, judge, threshold)  # noqa: E731
    if session_factory is None:
        from .chat import ChatSession

        session_factory = lambda: ChatSession(settings, skills)  # noqa: E731

    run = new_run("quality")
    run["summary"] = {"judge_model": judge_name, "threshold": threshold, "metrics": keys}
    passed = 0
    progress(log, "questions", 0, len(cases), message="", passed=0, failed=0)
    log(f"judge {judge_name}, threshold {threshold}, metrics: {', '.join(keys)}")
    for i, c in enumerate(cases):
        progress(log, "questions", i, len(cases), message=f"answering: {c['q'][:70]}", passed=passed, failed=i - passed)
        t0 = time.perf_counter()
        row: dict[str, Any] = {"q": c["q"], "skill": c.get("skill") or "", "expected_output": c.get("expected_output") or "", "metrics": {}}
        try:
            session = session_factory()
            ans = session.ask(c["q"], force_skill=c.get("skill"))
        except Exception as e:  # noqa: BLE001
            row.update({"answer": "", "got": f"ERROR {type(e).__name__}", "pass": False, "ms": int((time.perf_counter() - t0) * 1000), "cost_usd": 0.0, "detail": str(e)[:200]})
            run["rows"].append(row)
            log(f"BAD answer error: {str(e)[:120]}")
            continue
        answer_ms = int((time.perf_counter() - t0) * 1000)
        tools = sorted({tc.get("name", "") for tc in ans.tool_calls if tc.get("type") == "mcp_call" and tc.get("name")})
        progress(log, "questions", i, len(cases), message=f"judging: {c['q'][:70]}", passed=passed, failed=i - passed)
        t1 = time.perf_counter()
        scores = _score_case(keys, metric_factory, {"q": c["q"], "answer": ans.text, "expected_output": c.get("expected_output"), "retrieval_context": list(ans.retrieval_context), "tools_called": tools})
        judged = [s for s in scores.values() if s.get("success") is not None]
        ok = bool(judged) and all(s["success"] for s in judged)
        passed += ok
        row.update({"answer": ans.text, "got": ans.skill_id, "language": ans.language, "pass": ok, "ms": answer_ms, "judge_ms": int((time.perf_counter() - t1) * 1000),
                    "cost_usd": (ans.trace.get("cost") or {}).get("total_usd", 0.0), "metrics": scores, "tools_called": tools, "context_chunks": len(ans.retrieval_context),
                    "detail": "; ".join(f"{METRICS[k][1]} {s['score']:.2f}" for k, s in scores.items() if s.get("score") is not None)})
        run["rows"].append(row)
        log(f"{'ok ' if ok else 'BAD'} skill={ans.skill_id} " + " ".join(f"{k}={'-' if s.get('score') is None else f'{s['score']:.2f}'}" for k, s in scores.items()) + f" {c['q'][:50]}")
    n = len(cases)
    progress(log, "questions", n, n, message="", passed=passed, failed=n - passed)
    avg: dict[str, Optional[float]] = {}
    for k in keys:
        vals = [r["metrics"][k]["score"] for r in run["rows"] if r.get("metrics", {}).get(k, {}).get("score") is not None]
        avg[k] = (sum(vals) / len(vals)) if vals else None
    run["summary"].update({"questions": n, "passed": passed, "pass_rate": (passed / n) if n else 0.0, "total_cost_usd": sum(r.get("cost_usd", 0.0) for r in run["rows"]),
                           "avg_ms": int(sum(r.get("ms", 0) for r in run["rows"]) / n) if n else 0, "avg_scores": avg})
    run["finished_at"] = _now()
    return run
