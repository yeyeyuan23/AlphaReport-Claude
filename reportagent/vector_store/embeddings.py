from __future__ import annotations

import logging
import time
from functools import lru_cache
from typing import Optional, Any

from reportagent.utils.config import get_config, PROJECT_ROOT, get_llm_api_key, get_llm_base_url

logger = logging.getLogger(__name__)


class EmbeddingClient:
    """OpenAI-compatible embedding API wrapper.

    Supports both cloud (OpenAI / Voyage / DeepSeek via compatible endpoint)
    and local (sentence-transformers) backends.
    """

    def __init__(self, backend  : Optional[str] = None):
        self.backend = backend or get_config("vector_store", "embedding_backend", default="openai")

        if self.backend == "local":
            self._model = self._init_local()
            self._dim = get_config("vector_store", "local_dim", default=1024)
        elif self.backend == "voyage":
            self._api_key = get_config("vector_store", "voyage_api_key_env") or ""
            self._dim = get_config("vector_store", "voyage_dim", default=1024)
        else:
            self._client = self._init_openai()
            self._model_name = get_config("vector_store", "openai_model", default="text-embedding-3-small")
            self._dim = get_config("vector_store", "openai_dim", default=1536)

    # -- local ----------------------------------------------------------

    def _init_local(self):
        from sentence_transformers import SentenceTransformer

        model_name = get_config("vector_store", "local_model", default="BAAI/bge-small-zh-v1.5")
        logger.info("Loading local embedding model: %s", model_name)
        return SentenceTransformer(model_name)

    # -- openai-compatible ----------------------------------------------

    def _init_openai(self):
        from openai import OpenAI

        base_url = get_llm_base_url()
        api_key = get_llm_api_key()

        if base_url and "deepseek" in base_url:
            alt = get_config("vector_store", "openai_base_url")
            if alt:
                base_url = alt
            else:
                logger.warning(
                    "DeepSeek API does not support embeddings. "
                    "Set vector_store.openai_base_url in app.yaml or use backend=local/voyage."
                )

        kwargs: dict[str, Any] = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        return OpenAI(**kwargs)

    @property
    def dimension(self) -> int:
        return self._dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a list of texts, returns list of float vectors."""
        if not texts:
            return []

        if self.backend == "local":
            return self._embed_local(texts)
        elif self.backend == "voyage":
            return self._embed_voyage(texts)
        else:
            return self._embed_openai(texts)

    def _embed_local(self, texts: list[str]) -> list[list[float]]:
        result = self._model.encode(texts, normalize_embeddings=True)
        return result.tolist()

    def _embed_voyage(self, texts: list[str]) -> list[list[float]]:
        import requests

        api_key = self._api_key or get_llm_api_key()
        resp = requests.post(
            "https://api.voyageai.com/v1/embeddings",
            json={
                "model": get_config("vector_store", "voyage_model", default="voyage-3-lite"),
                "input": texts,
            },
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        return [d["embedding"] for d in data["data"]]

    def _embed_openai(self, texts: list[str]) -> list[list[float]]:
        max_retries = 3
        for attempt in range(max_retries):
            try:
                resp = self._client.embeddings.create(
                    model=self._model_name,
                    input=texts,
                )
                return [d.embedding for d in resp.data]
            except Exception as e:
                logger.warning("Embedding attempt %d failed: %s", attempt + 1, e)
                if attempt == max_retries - 1:
                    raise
                time.sleep(2 ** attempt)


@lru_cache(maxsize=1)
def get_embedding_client() -> EmbeddingClient:
    return EmbeddingClient()
