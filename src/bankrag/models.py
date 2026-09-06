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
    turns: list[Turn] = Field(default_factory=list)
