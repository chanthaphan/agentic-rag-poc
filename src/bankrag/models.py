"""Pydantic models shared across modules."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

AGENT_PREFIX = "bank-"
KS_PREFIX = "ks-"
KB_PREFIX = "kb-"
CONNECTION_SUFFIX = "-mcp"
ROUTER_AGENT = "bank-router"


class SkillSpec(BaseModel):
    id: str
    name: str
    description: str
    product_category: str
    keywords: list[str] = Field(default_factory=list)
    model: Optional[str] = None
    top_k: int = 5
    filter: Optional[str] = None  # None -> default category filter; "" -> no filter
    version: int = 1
    suggestions: list[str] = Field(default_factory=list)  # follow-up prompts shown under answers
    body: str = ""  # markdown instructions (frontmatter removed)
    path: Optional[Path] = None

    @property
    def agent_name(self) -> str:
        return f"{AGENT_PREFIX}{self.id}"

    @property
    def ks_name(self) -> str:
        return f"{KS_PREFIX}{self.id}"

    @property
    def kb_name(self) -> str:
        return f"{KB_PREFIX}{self.id}"

    @property
    def connection_name(self) -> str:
        return f"{self.kb_name}{CONNECTION_SUFFIX}"

    @property
    def effective_filter(self) -> str:
        if self.filter is None:
            if self.product_category in ("", "all", "*"):
                return ""
            return f"product_category eq '{self.product_category}'"
        return self.filter


class RuleProduct(BaseModel):
    """One regulated product family from the rules sheet (column 'ผลิตภัณฑ์ที่ต้องตรวจสอบ')."""

    id: str
    name: str  # Thai name exactly as the compliance team writes it in the sheet
    aliases: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)  # skill ids that answer about this family
    match: list[str] = Field(default_factory=list)  # regex detecting the family in a question or an answer


class RuleSpec(BaseModel):
    """A Responsible Lending rule: the regulator's text plus what the app needs to apply and check it."""

    id: str
    pack: str = ""
    title: str = ""
    regulation: str = ""  # เล่มกฎหมาย
    clause: str = ""  # ข้อกฎหมาย
    products: list[str] = Field(default_factory=list)  # RuleProduct ids
    status: str = "active"  # active | draft | retired
    severity: str = "block"  # block: the answer must not go out without it | warn
    check: str = "judgement"  # required_phrase | prohibited_phrase | required_pattern | judgement
    enforcement: str = "flag"  # append (add the mandated wording) | flag | none
    phrases: list[str] = Field(default_factory=list)  # required / prohibited wording
    patterns: list[str] = Field(default_factory=list)  # regex for required_pattern and wording variants
    applies_when: list[str] = Field(default_factory=list)  # regex on the answer; empty = whenever a product matches
    disclosure: dict[str, str] = Field(default_factory=dict)  # th / en text appended when enforcement == append
    template: str = ""  # the wording the answer must carry (shown to the agent and the reviewer)
    legal_text: str = ""  # กฎหมาย, verbatim
    system_rule: str = ""  # กฎสำหรับระบบ, verbatim: the compliance team's instruction
    assistant_note: str = ""  # how the rule applies to a chat answer; written by us, kept across sheet imports
    extra_body: str = ""  # any other markdown section in the file, kept verbatim so a save never drops it
    path: Optional[Path] = None

    @property
    def label(self) -> str:
        return f"{self.clause} {self.title}".strip()


class RulePack(BaseModel):
    id: str
    name: str = ""
    description: str = ""
    sources: list[str] = Field(default_factory=list)
    products: list[RuleProduct] = Field(default_factory=list)
    body: str = ""
    rules: list[RuleSpec] = Field(default_factory=list)
    path: Optional[Path] = None

    def product(self, pid: str) -> Optional[RuleProduct]:
        return next((p for p in self.products if p.id == pid), None)


class RuleFinding(BaseModel):
    rule_id: str
    clause: str = ""
    title: str = ""
    products: list[str] = Field(default_factory=list)
    severity: str = "block"
    verdict: str = "compliant"  # compliant | non_compliant | undefined | not_applicable
    detail: str = ""
    fixed: bool = False  # the mandated wording was appended to the answer


class Chunk(BaseModel):
    id: str
    doc_id: str
    content: str
    title: str
    breadcrumb: str = ""
    product_category: str
    product_name: str = ""
    doc_type: str = "product-page"
    source_url: str = ""
    source_file: str = ""
    chunk_index: int
    language: str = "th"
    content_hash: str = ""
    last_updated: str = ""


class RouteDecision(BaseModel):
    skill_id: str
    confidence: float = 0.0
    language: str = "th"
    reason: str = ""
    usage: dict[str, Any] = Field(default_factory=dict)
    elapsed_ms: int = 0


class Citation(BaseModel):
    title: str = ""
    url: str = ""


class Reference(BaseModel):
    id: str = ""
    title: str = ""
    source_url: str = ""
    product_name: str = ""
    doc_type: str = ""
    snippet: str = ""
    score: Optional[float] = None


class Answer(BaseModel):
    skill_id: str
    confidence: float
    route_reason: str = ""
    text: str
    language: str = "th"  # th | en : language the user wrote in (answer + suggestions follow it)
    suggestions: list[str] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)
    references: list[Reference] = Field(default_factory=list)
    agent_name: str = ""
    agent_version: str = ""
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    conversation_id: str = ""
    trace: dict[str, Any] = Field(default_factory=dict)  # timings, token usage, retrieval stats, reasoning
    retrieval_context: list[str] = Field(default_factory=list, exclude=True)  # full knowledge-base tool outputs; evals only, never stored


class IngestDocReport(BaseModel):
    doc_id: str
    action: str  # added | updated | unchanged | deleted | skipped
    chunks: int = 0
    note: str = ""


class IngestReport(BaseModel):
    docs: list[IngestDocReport] = Field(default_factory=list)
    uploaded_chunks: int = 0
    deleted_chunks: int = 0
    per_category: dict[str, int] = Field(default_factory=dict)
    dry_run: bool = False

    def summary(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for d in self.docs:
            out[d.action] = out.get(d.action, 0) + 1
        return out


class SyncRow(BaseModel):
    skill_id: str
    knowledge_source: str = ""
    knowledge_base: str = ""
    connection: str = ""
    agent: str = ""
    action: str = ""  # created | unchanged | error | pruned | skipped
    version: str = ""
    note: str = ""


class SyncReport(BaseModel):
    rows: list[SyncRow] = Field(default_factory=list)


class Turn(BaseModel):
    role: str  # user | assistant
    text: str
    at: str = ""
    by: str = ""  # display name of the signed-in person who typed a user turn (from SSO), if known
    skill_id: str = ""
    confidence: Optional[float] = None
    citations: list[Citation] = Field(default_factory=list)
    references: list[Reference] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)
    language: str = ""
    route_reason: str = ""
    agent_name: str = ""
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    trace: dict[str, Any] = Field(default_factory=dict)


class SessionRecord(BaseModel):
    id: str
    created_at: str
    updated_at: str
    title: str = ""
    conversation_id: Optional[str] = None
    prev_skill: Optional[str] = None
    source: str = "app"  # app | studio | eval
    user_name: str = ""  # signed-in person who started the session (SSO display name), if known
    user_email: str = ""
    turns: list[Turn] = Field(default_factory=list)
