"""Client-side embeddings through the Azure OpenAI v1 endpoint (same deployment the index vectorizer uses)."""
from __future__ import annotations

import time

from openai import OpenAI, RateLimitError, APIStatusError

from ..config import Settings


def make_openai_client(settings: Settings) -> OpenAI:
    settings.require("aoai_endpoint", "aoai_api_key")
    return OpenAI(base_url=settings.aoai_v1_base_url, api_key=settings.aoai_api_key)


def embed_texts(texts: list[str], settings: Settings, *, batch_size: int = 32, client: OpenAI | None = None, on_progress=None) -> list[list[float]]:
    client = client or make_openai_client(settings)
    out: list[list[float]] = []
    for i in range(0, len(texts), batch_size):
        batch = texts[i : i + batch_size]
        for attempt in range(6):
            try:
                resp = client.embeddings.create(model=settings.embed_deployment, input=batch)
                vectors = [d.embedding for d in sorted(resp.data, key=lambda d: d.index)]
                if vectors and len(vectors[0]) != settings.embed_dims:
                    raise RuntimeError(
                        f"embedding dims {len(vectors[0])} != EMBED_DIMS {settings.embed_dims}; fix .env or the index"
                    )
                out.extend(vectors)
                if on_progress:
                    on_progress(len(out), len(texts))
                break
            except (RateLimitError, APIStatusError) as e:  # retry on 429/5xx
                status = getattr(e, "status_code", 0) or 0
                if attempt == 5 or (status and status < 500 and status != 429):
                    raise
                time.sleep(2**attempt)
    return out
