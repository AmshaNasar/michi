"""Embedding generation for semantic recall.

Optional by design: if no embedding provider is configured the memory layer
falls back to keyword search, so the assistant still runs end to end on a
machine with only an Anthropic key.
"""

from typing import List, Optional

import httpx

from twin.config import SETTINGS

_OPENAI_EMBEDDINGS_URL = "https://api.openai.com/v1/embeddings"


def embed(text: str) -> Optional[List[float]]:
    """Return an embedding vector, or None when embeddings are disabled."""
    if not SETTINGS.embeddings_enabled:
        return None

    response = httpx.post(
        _OPENAI_EMBEDDINGS_URL,
        headers={"Authorization": "Bearer {0}".format(SETTINGS.openai_api_key)},
        json={"model": SETTINGS.embedding_model, "input": text[:8000]},
        timeout=30.0,
    )
    response.raise_for_status()
    return response.json()["data"][0]["embedding"]
