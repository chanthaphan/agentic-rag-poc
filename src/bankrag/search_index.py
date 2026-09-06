"""Azure AI Search index for all product documents (one index, filtered per skill)."""
from __future__ import annotations

from typing import Iterable

from azure.core.credentials import AzureKeyCredential
from azure.search.documents import SearchClient
from azure.search.documents.indexes import SearchIndexClient
from azure.search.documents.indexes.models import (
    AzureOpenAIVectorizer,
    AzureOpenAIVectorizerParameters,
    HnswAlgorithmConfiguration,
    SearchField,
    SearchFieldDataType,
    SearchIndex,
    SemanticConfiguration,
    SemanticField,
    SemanticPrioritizedFields,
    SemanticSearch,
    VectorSearch,
    VectorSearchProfile,
)

from .config import Settings

SEMANTIC_CONFIG = "default"
VECTOR_PROFILE = "hnsw-aoai"
VECTOR_ALGO = "hnsw"
VECTORIZER = "aoai-embed"
THAI_ANALYZER = "th.microsoft"


def index_client(settings: Settings) -> SearchIndexClient:
    settings.require("search_endpoint", "search_admin_key")
    return SearchIndexClient(settings.search_endpoint, AzureKeyCredential(settings.search_admin_key), api_version=settings.search_api_version)


def search_client(settings: Settings) -> SearchClient:
    settings.require("search_endpoint", "search_admin_key")
    return SearchClient(settings.search_endpoint, settings.search_index, AzureKeyCredential(settings.search_admin_key), api_version=settings.search_api_version)


def build_index(settings: Settings) -> SearchIndex:
    settings.require("aoai_endpoint", "aoai_api_key")
    fields = [
        SearchField(name="id", type=SearchFieldDataType.String, key=True, filterable=True),
        SearchField(name="doc_id", type=SearchFieldDataType.String, filterable=True),
        SearchField(name="content", type=SearchFieldDataType.String, searchable=True, analyzer_name=THAI_ANALYZER),
        SearchField(
            name="content_vector",
            type=SearchFieldDataType.Collection(SearchFieldDataType.Single),
            searchable=True,
            stored=False,
            vector_search_dimensions=settings.embed_dims,
            vector_search_profile_name=VECTOR_PROFILE,
        ),
        SearchField(name="title", type=SearchFieldDataType.String, searchable=True, analyzer_name=THAI_ANALYZER),
        SearchField(name="breadcrumb", type=SearchFieldDataType.String, searchable=True, analyzer_name=THAI_ANALYZER),
        SearchField(name="product_category", type=SearchFieldDataType.String, filterable=True, facetable=True),
        SearchField(name="product_name", type=SearchFieldDataType.String, filterable=True, facetable=True, searchable=True, analyzer_name=THAI_ANALYZER),
        SearchField(name="doc_type", type=SearchFieldDataType.String, filterable=True, facetable=True),
        SearchField(name="source_url", type=SearchFieldDataType.String),
        SearchField(name="source_file", type=SearchFieldDataType.String, filterable=True),
        SearchField(name="chunk_index", type=SearchFieldDataType.Int32, sortable=True),
        SearchField(name="language", type=SearchFieldDataType.String, filterable=True, facetable=True),
        SearchField(name="last_updated", type=SearchFieldDataType.DateTimeOffset, filterable=True, sortable=True),
        SearchField(name="content_hash", type=SearchFieldDataType.String),
    ]
    vector_search = VectorSearch(
        algorithms=[HnswAlgorithmConfiguration(name=VECTOR_ALGO)],
        profiles=[VectorSearchProfile(name=VECTOR_PROFILE, algorithm_configuration_name=VECTOR_ALGO, vectorizer_name=VECTORIZER)],
        vectorizers=[
            AzureOpenAIVectorizer(
                vectorizer_name=VECTORIZER,
                parameters=AzureOpenAIVectorizerParameters(
                    resource_url=settings.aoai_endpoint,
                    deployment_name=settings.embed_deployment,
                    model_name=settings.embed_deployment,
                    api_key=settings.aoai_api_key,
                ),
            )
        ],
    )
    semantic = SemanticSearch(
        default_configuration_name=SEMANTIC_CONFIG,
        configurations=[
            SemanticConfiguration(
                name=SEMANTIC_CONFIG,
                prioritized_fields=SemanticPrioritizedFields(
                    title_field=SemanticField(field_name="title"),
                    content_fields=[SemanticField(field_name="content")],
                    keywords_fields=[SemanticField(field_name="breadcrumb"), SemanticField(field_name="product_name")],
                ),
            )
        ],
    )
    return SearchIndex(name=settings.search_index, fields=fields, vector_search=vector_search, semantic_search=semantic)


def ensure_index(settings: Settings) -> SearchIndex:
    return index_client(settings).create_or_update_index(build_index(settings))


def delete_index(settings: Settings) -> None:
    index_client(settings).delete_index(settings.search_index)


def facet_counts(settings: Settings, field: str = "product_category") -> dict[str, int]:
    res = search_client(settings).search(search_text="*", facets=[f"{field},count:100"], top=0, include_total_count=True)
    facets = res.get_facets() or {}
    return {f["value"]: f["count"] for f in facets.get(field, [])}


def index_stats(settings: Settings) -> dict:
    stats = index_client(settings).get_index_statistics(settings.search_index)
    if isinstance(stats, dict):
        return dict(stats)
    return {k: getattr(stats, k, None) for k in ("document_count", "storage_size", "vector_index_size")}


def upload_chunks(settings: Settings, docs: Iterable[dict], batch_size: int = 100, on_progress=None) -> int:
    client = search_client(settings)
    docs = list(docs)
    n = 0
    for i in range(0, len(docs), batch_size):
        results = client.merge_or_upload_documents(documents=docs[i : i + batch_size])
        failed = [r for r in results if not r.succeeded]
        if failed:
            raise RuntimeError(f"{len(failed)} documents failed to upload, first: {failed[0].error_message}")
        n += len(results)
        if on_progress:
            on_progress(n, len(docs))
    return n


def delete_chunks(settings: Settings, ids: Iterable[str]) -> int:
    ids = list(ids)
    if not ids:
        return 0
    client = search_client(settings)
    n = 0
    for i in range(0, len(ids), 500):
        results = client.delete_documents(documents=[{"id": _id} for _id in ids[i : i + 500]])
        n += sum(1 for r in results if r.succeeded)
    return n


def get_documents(settings: Settings, ids: Iterable[str], fields: tuple[str, ...] = ("id", "title", "source_url", "product_name", "doc_type")) -> dict[str, dict]:
    """Fetch selected fields for chunk ids (used to resolve citations to their original URLs)."""
    client = search_client(settings)
    out: dict[str, dict] = {}
    for _id in ids:
        try:
            out[_id] = dict(client.get_document(key=_id, selected_fields=list(fields)))
        except Exception:  # noqa: BLE001 - missing doc just stays unresolved
            continue
    return out


def chunks_for_doc(settings: Settings, doc_id: str) -> list[dict]:
    """All indexed chunks of one document (knowledge-relative path), ordered by chunk_index."""
    client = search_client(settings)
    safe = doc_id.replace("'", "''")
    res = client.search(search_text="*", filter=f"doc_id eq '{safe}'", select=["id", "chunk_index", "breadcrumb", "content", "title"], order_by=["chunk_index asc"], top=500)
    return [dict(r) for r in res]


def hybrid_search(settings: Settings, query: str, *, category: str | None = None, k: int = 5) -> list[dict]:
    """Hybrid (keyword + vector via the index vectorizer) + semantic reranking, optionally filtered by category."""
    from azure.search.documents.models import VectorizableTextQuery

    client = search_client(settings)
    flt = f"product_category eq '{category}'" if category and category not in ("all", "*") else None
    res = client.search(
        search_text=query,
        vector_queries=[VectorizableTextQuery(text=query, k_nearest_neighbors=k * 4, fields="content_vector")],
        filter=flt,
        select=["id", "doc_id", "chunk_index", "breadcrumb", "content", "title", "source_url", "product_name"],
        query_type="semantic",
        semantic_configuration_name=SEMANTIC_CONFIG,
        top=k,
    )
    out = []
    for r in res:
        d = dict(r)
        out.append({"id": d.get("id"), "doc_id": d.get("doc_id"), "chunk_index": d.get("chunk_index"), "breadcrumb": d.get("breadcrumb", ""), "title": d.get("title", ""),
                    "source_url": d.get("source_url", ""), "product_name": d.get("product_name", ""), "snippet": (d.get("content") or "")[:400],
                    "score": d.get("@search.score"), "reranker_score": d.get("@search.reranker_score")})
    return out
