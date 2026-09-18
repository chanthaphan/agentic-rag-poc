"""Foundry IQ objects in Azure AI Search: one knowledge source + one knowledge base per skill."""
from __future__ import annotations

from typing import Optional

from azure.core.credentials import AzureKeyCredential
from azure.core.exceptions import ResourceNotFoundError
from azure.search.documents.indexes import SearchIndexClient
from azure.search.documents.indexes.models import (
    AzureOpenAIVectorizerParameters,
    KnowledgeBase,
    KnowledgeBaseAzureOpenAIModel,
    KnowledgeBaseRetrieveDefaults,
    KnowledgeSourceReference,
    SearchIndexFieldReference,
    SearchIndexKnowledgeSource,
    SearchIndexKnowledgeSourceParameters,
)
from azure.search.documents.knowledgebases import KnowledgeBaseRetrievalClient
from azure.search.documents.knowledgebases.models import (
    KnowledgeBaseMessage,
    KnowledgeBaseMessageTextContent,
    KnowledgeBaseRetrievalRequest,
    KnowledgeRetrievalLowReasoningEffort,
    KnowledgeRetrievalMediumReasoningEffort,
    KnowledgeRetrievalMinimalReasoningEffort,
    KnowledgeRetrievalSemanticIntent,
    SearchIndexKnowledgeSourceParams,
)

from .config import Settings
from .models import Reference, SkillSpec
from .search_index import SEMANTIC_CONFIG, index_client

SOURCE_DATA_FIELDS = ["id", "title", "source_url", "product_name", "doc_type", "breadcrumb", "content"]
SEARCH_FIELDS = ["content", "title", "breadcrumb"]


def _reasoning(kind: str):
    return {
        "minimal": KnowledgeRetrievalMinimalReasoningEffort,
        "low": KnowledgeRetrievalLowReasoningEffort,
        "medium": KnowledgeRetrievalMediumReasoningEffort,
    }.get(kind, KnowledgeRetrievalMinimalReasoningEffort)()


def build_knowledge_source(settings: Settings, spec: SkillSpec) -> SearchIndexKnowledgeSource:
    params = SearchIndexKnowledgeSourceParameters(
        search_index_name=settings.search_index,
        semantic_configuration_name=SEMANTIC_CONFIG,
        source_data_fields=[SearchIndexFieldReference(name=f) for f in SOURCE_DATA_FIELDS],
        search_fields=[SearchIndexFieldReference(name=f) for f in SEARCH_FIELDS],
    )
    if spec.effective_filter:
        params.base_filter = spec.effective_filter
    return SearchIndexKnowledgeSource(
        name=spec.ks_name,
        description=f"bankrag skill '{spec.id}' ({spec.product_category}) over index {settings.search_index}",
        search_index_parameters=params,
    )


def build_knowledge_base(settings: Settings, spec: SkillSpec, *, reasoning: Optional[str] = None) -> KnowledgeBase:
    reasoning = reasoning or settings.kb_reasoning_effort
    kb = KnowledgeBase(
        name=spec.kb_name,
        description=f"bankrag knowledge base for skill '{spec.id}'",
        knowledge_sources=[KnowledgeSourceReference(name=spec.ks_name)],
        retrieval_reasoning_effort=_reasoning(reasoning),
        retrieve_defaults=KnowledgeBaseRetrieveDefaults(max_output_documents=spec.top_k, max_output_size_in_tokens=settings.kb_max_output_tokens or None),
    )
    if reasoning != "minimal":
        kb.models = [
            KnowledgeBaseAzureOpenAIModel(
                azure_open_ai_parameters=AzureOpenAIVectorizerParameters(
                    resource_url=settings.aoai_endpoint,
                    deployment_name=settings.kb_llm_deployment,
                    model_name=settings.kb_llm_deployment,
                    api_key=settings.aoai_api_key,
                )
            )
        ]
    return kb


def ensure_knowledge_objects(settings: Settings, spec: SkillSpec, *, client: SearchIndexClient | None = None) -> tuple[str, str]:
    client = client or index_client(settings)
    ks = client.create_or_update_knowledge_source(build_knowledge_source(settings, spec))
    kb = client.create_or_update_knowledge_base(build_knowledge_base(settings, spec))
    return ks.name, kb.name


def delete_knowledge_objects(settings: Settings, spec: SkillSpec, *, client: SearchIndexClient | None = None) -> None:
    client = client or index_client(settings)
    for fn, name in ((client.delete_knowledge_base, spec.kb_name), (client.delete_knowledge_source, spec.ks_name)):
        try:
            fn(name)
        except ResourceNotFoundError:
            pass


def category_has_documents(settings: Settings, spec: SkillSpec, facets: Optional[dict[str, int]] = None) -> bool:
    """True when the skill's category has files on disk or indexed chunks. `general` always counts."""
    if spec.product_category in ("all", "*", ""):
        return True
    from .ingest.pipeline import discover  # local import to keep this module light

    if any(cat == spec.product_category for cat, _ in discover(settings.knowledge_dir, spec.product_category)):
        return True
    return bool((facets or {}).get(spec.product_category, 0))


def shared_kb_spec(skills: dict[str, SkillSpec]) -> Optional[SkillSpec]:
    """The skill whose knowledge base is shared by skills that have no documents yet (the unfiltered one)."""
    for spec in skills.values():
        if not spec.effective_filter:
            return spec
    return None


def list_knowledge_bases(settings: Settings) -> list[str]:
    return [kb.name for kb in index_client(settings).list_knowledge_bases()]


def retrieve(settings: Settings, kb_name: str, question: str, *, ks_name: Optional[str] = None, max_docs: Optional[int] = None,
             variants: Optional[list[str]] = None) -> list[Reference]:
    """One retrieve call on the knowledge base: the documents for the agent, the Sources panel and debugging.
    `variants` are extra search intents (the model's rewordings) when the base does no query planning of its own."""
    settings.require("search_endpoint", "search_admin_key")
    client = KnowledgeBaseRetrievalClient(
        settings.search_endpoint, AzureKeyCredential(settings.search_admin_key), knowledge_base_name=kb_name, api_version=settings.search_api_version
    )
    if settings.kb_reasoning_effort == "minimal":
        # minimal effort = no query planning: the caller supplies the search intents directly
        seen: list[str] = []
        for q in [question, *(variants or [])]:
            q = str(q or "").strip()
            if q and q not in seen:
                seen.append(q)
        req = KnowledgeBaseRetrievalRequest(intents=[KnowledgeRetrievalSemanticIntent(search=q) for q in seen], include_activity=False)
    else:
        req = KnowledgeBaseRetrievalRequest(
            messages=[KnowledgeBaseMessage(role="user", content=[KnowledgeBaseMessageTextContent(text=question)])],
            include_activity=False,
        )
    if max_docs:
        req.max_output_documents = max_docs
    if ks_name:
        req.knowledge_source_params = [
            SearchIndexKnowledgeSourceParams(knowledge_source_name=ks_name, include_references=True, include_reference_source_data=True)
        ]
    resp = client.retrieve(req)
    refs: list[Reference] = []
    for r in resp.references or []:
        sd = getattr(r, "source_data", None) or {}
        if hasattr(sd, "as_dict"):
            sd = sd.as_dict()
        sd = dict(sd) if isinstance(sd, dict) else {}
        content = str(sd.get("content") or "")
        refs.append(
            Reference(
                id=str(getattr(r, "doc_key", None) or getattr(r, "id", "")),
                title=str(sd.get("title", "")),
                source_url=str(sd.get("source_url", "") or getattr(r, "citation_url", "") or ""),
                product_name=str(sd.get("product_name", "")),
                doc_type=str(sd.get("doc_type", "")),
                snippet=content[:400],
                content=content,
                score=getattr(r, "reranker_score", None),
            )
        )
    return refs
