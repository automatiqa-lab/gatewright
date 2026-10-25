"""ModelProvider adapters.

Adapters only call models. Redaction and the external-call hard limit live in
`gw.engines.models.ModelGateway`, which every engine uses. Chat-capable language models
(`ollama` here; `openai_compatible` and `anthropic_api` in llm.py) also satisfy `ChatModel`.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from gw.harness.contracts import ChatRequest, ChatResponse, Health, Prediction
from gw.harness.registry import register, resolve_config
from gw.record import models as m


@register("local_models")
class LocalModels:
    """Offline default: hashing char n-gram embeddings, no LLM (rules and regex extraction only)."""

    kind = "embedding"

    def __init__(self, conn: m.Connection | None = None, dim: int = 512) -> None:
        self.dim = dim

    def complete(self, prompt: str, schema: dict[str, Any] | None, max_tokens: int) -> str | dict[str, Any]:
        return {} if schema else ""

    def embed(self, texts: list[str]) -> list[list[float]]:
        from sklearn.feature_extraction.text import HashingVectorizer

        hv = HashingVectorizer(analyzer="char_wb", ngram_range=(3, 4), n_features=self.dim, alternate_sign=False,
                               norm="l2")
        return [list(map(float, row)) for row in hv.transform([t.lower() for t in texts]).toarray()]

    def tabular_fit_predict(self, X_ctx: Any, y_ctx: Any, X_query: Any, task: str) -> Prediction:
        raise NotImplementedError("use a tabular adapter (catboost, mitra_v2)")

    def is_external(self) -> bool:
        return False

    def health(self) -> Health:
        return Health(status="ok", detail="local hashing embeddings")


@register("ollama")
class Ollama:
    """Local LLM and embeddings via an Ollama server (JSON schema output)."""

    kind = "llm"

    def __init__(self, conn: m.Connection) -> None:
        cfg = resolve_config(conn.config)
        self.base = cfg.get("base_url", "http://localhost:11434")
        self.llm = cfg.get("llm", "llama3.1:8b")
        self.embedding = cfg.get("embedding", "bge-m3")
        self.retries = int(cfg.get("retries", 2))
        self.num_ctx = int(cfg.get("num_ctx", 16384))
        self.client = httpx.Client(timeout=float(cfg.get("timeout", 120)))

    def chat(self, request: ChatRequest) -> ChatResponse:
        from gw.harness.adapters.llm import ollama_chat

        return ollama_chat(self.client, self.base, self.llm, request, self.retries, self.num_ctx)

    def complete(self, prompt: str, schema: dict[str, Any] | None, max_tokens: int) -> str | dict[str, Any]:
        body: dict[str, Any] = {"model": self.llm, "prompt": prompt, "stream": False, "options": {"num_predict": max_tokens}}
        if schema:
            body["format"] = schema
        r = self.client.post(f"{self.base}/api/generate", json=body)
        r.raise_for_status()
        text = r.json().get("response", "")
        if schema:
            try:
                return dict(json.loads(text))
            except ValueError:
                return {}
        return str(text)

    def embed(self, texts: list[str]) -> list[list[float]]:
        r = self.client.post(f"{self.base}/api/embed", json={"model": self.embedding, "input": texts})
        r.raise_for_status()
        return [list(map(float, v)) for v in r.json()["embeddings"]]

    def tabular_fit_predict(self, X_ctx: Any, y_ctx: Any, X_query: Any, task: str) -> Prediction:
        raise NotImplementedError

    def is_external(self) -> bool:
        from gw.harness.adapters.llm import is_local_url

        return not is_local_url(self.base)  # an Ollama on another host is an external provider

    def health(self) -> Health:
        try:
            self.client.get(f"{self.base}/api/tags").raise_for_status()
            return Health(status="ok", detail=self.base)
        except Exception as e:
            return Health(status="down", detail=str(e))


@register("sentence_transformers")
class SentenceTransformers:
    """Local embeddings (bge-small-en by default; bge-m3 for multilingual corpora). Optional extra."""

    kind = "embedding"

    def __init__(self, conn: m.Connection) -> None:
        from sentence_transformers import SentenceTransformer

        self.model = SentenceTransformer(conn.config.get("model", "BAAI/bge-small-en-v1.5"))

    def complete(self, prompt: str, schema: dict[str, Any] | None, max_tokens: int) -> str | dict[str, Any]:
        raise NotImplementedError

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [list(map(float, v)) for v in self.model.encode(texts, normalize_embeddings=True)]

    def tabular_fit_predict(self, X_ctx: Any, y_ctx: Any, X_query: Any, task: str) -> Prediction:
        raise NotImplementedError

    def is_external(self) -> bool:
        return False

    def health(self) -> Health:
        return Health(status="ok")
