"""Language-model adapters behind the model-neutral chat contract.

The engine speaks `ChatRequest` / `ChatResponse` only (gw/harness/contracts.py). Each adapter
translates to one wire format; model names live in connection config, never in engine code.

- `ollama`: local server (`/api/chat`); not external. Defined in models.py, chat added there.
- `openai_compatible`: any `/v1/chat/completions` endpoint (vLLM, LM Studio, llama.cpp server,
  Azure OpenAI, Mistral, OpenRouter, ...). External unless the connection says `external: false`.
- `anthropic_api`: Anthropic Messages API through the official SDK (extra `gatewright[anthropic]`).

Every adapter: bounded timeout, retries with backoff on 429 / 5xx / connection errors, provider
errors surface as `ModelError`, and a refusal or length cut is reported in `stop`, never hidden.
Hard limit 3 (redaction for external providers) is enforced in `gw.engines.models.ModelGateway`.
"""

from __future__ import annotations

import json
import random
import time
from typing import Any, Literal

import httpx

from gw.harness.contracts import (
    ChatMessage,
    ChatRequest,
    ChatResponse,
    Health,
    Prediction,
    ToolCall,
    Usage,
)
from gw.harness.registry import register, resolve_config, secret
from gw.record import models as m

RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 529}
Stop = Literal["end", "tool", "length", "refused", "error"]
STOP_OPENAI: dict[str, Stop] = {"stop": "end", "tool_calls": "tool", "length": "length", "content_filter": "refused"}
STOP_ANTHROPIC: dict[str, Stop] = {"end_turn": "end", "tool_use": "tool", "max_tokens": "length", "refusal": "refused",
                                   "stop_sequence": "end", "pause_turn": "end",
                                   "model_context_window_exceeded": "length"}


class ModelError(RuntimeError):
    """A provider call failed after retries (status, timeout or malformed reply)."""


def is_local_url(url: str) -> bool:
    """True only for loopback, private-range or .local hosts. Anything else is an external provider,
    whatever its connection says, so hard limit 3 cannot be configured away."""
    import ipaddress
    import socket
    from urllib.parse import urlparse

    host = (urlparse(url).hostname or "").lower()
    if not host:
        return False
    if host == "localhost" or host.endswith(".local") or host.endswith(".internal"):
        return True
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        try:
            addr = ipaddress.ip_address(socket.gethostbyname(host))
        except OSError:
            return False
    local = [ipaddress.ip_network(n) for n in (
        "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",  # RFC 1918
        "100.64.0.0/10",  # private overlay networks (e.g. a tailnet)
        "169.254.0.0/16", "fc00::/7", "fe80::/10")]  # link-local, IPv6 unique-local
    return addr.is_loopback or any(addr in n for n in local if n.version == addr.version)


def post_json(client: httpx.Client, url: str, body: dict[str, Any], headers: dict[str, str] | None = None,
              retries: int = 2) -> dict[str, Any]:
    """POST with exponential backoff and Retry-After on throttling and server errors."""
    last: Exception | None = None
    where = url.split("?")[0]
    for attempt in range(retries + 1):
        try:
            r = client.post(url, json=body, headers=headers or {})
        except httpx.ReadTimeout as e:  # the provider may still be working (and billing): do not resend
            raise ModelError(f"{where}: read timeout") from e
        except httpx.TransportError as e:  # nothing reached the model: safe to retry
            last = e
            if attempt < retries:
                time.sleep(min(2 ** attempt + random.random(), 20))
            continue
        if r.status_code in RETRY_STATUS and attempt < retries:
            try:
                wait = float(r.headers.get("retry-after") or 0)
            except ValueError:
                wait = 0.0
            time.sleep(min(wait if wait > 0 else 2 ** attempt + random.random(), 30))
            continue
        if r.status_code >= 400:
            raise ModelError(f"{where}: HTTP {r.status_code} {r.text[:200]}")
        try:
            data = r.json()
        except ValueError as e:
            raise ModelError(f"{where}: reply is not JSON") from e
        if not isinstance(data, dict):
            raise ModelError(f"{where}: unexpected reply")
        return data
    raise ModelError(f"{where}: {type(last).__name__}: {last}")


class _ChatOnly:
    """Shared non-chat parts: `complete` over chat, no embeddings or tabular models."""

    kind = "llm"

    def chat(self, request: ChatRequest) -> ChatResponse:
        raise NotImplementedError

    def complete(self, prompt: str, schema: dict[str, Any] | None, max_tokens: int) -> str | dict[str, Any]:
        res = self.chat(ChatRequest(messages=[ChatMessage(role="user", content=prompt)], json_schema=schema,
                                    max_tokens=max_tokens))
        return res.parsed() if schema else res.text

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError("use an embedding connection")

    def tabular_fit_predict(self, X_ctx: Any, y_ctx: Any, X_query: Any, task: str) -> Prediction:
        raise NotImplementedError


# --- OpenAI-compatible ---------------------------------------------------------------------------


@register("openai_compatible")
class OpenAICompatible(_ChatOnly):
    """Config: {base_url, model, [secret_ref], [external: true], [timeout: 120], [retries: 2],
    [max_tokens_field: max_tokens | max_completion_tokens], [structured_output: true]}."""

    def __init__(self, conn: m.Connection, transport: httpx.BaseTransport | None = None) -> None:
        cfg = resolve_config(conn.config)
        self.conn = conn
        self.base = str(cfg.get("base_url", "")).rstrip("/")
        self.model = str(cfg.get("model", ""))
        # local only when the connection says so AND the URL really is local or private
        self.external = bool(cfg.get("external", True)) or not is_local_url(self.base)
        self.retries = int(cfg.get("retries", 2))
        self.max_field = str(cfg.get("max_tokens_field", "max_tokens"))
        self.structured = bool(cfg.get("structured_output", True))
        self._key = secret(cfg.get("secret_ref")) if cfg.get("secret_ref") else None
        self.client = httpx.Client(timeout=float(cfg.get("timeout", 120)), transport=transport)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._key}"} if self._key else {}

    @staticmethod
    def _messages(req: ChatRequest) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = [{"role": "system", "content": req.system}] if req.system else []
        for msg in req.messages:
            if msg.role == "tool":
                out.append({"role": "tool", "tool_call_id": msg.tool_call_id, "content": msg.content})
            elif msg.role == "assistant" and msg.tool_calls:
                out.append({"role": "assistant", "content": msg.content or None, "tool_calls": [
                    {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": json.dumps(c.arguments)}}
                    for c in msg.tool_calls]})
            else:
                out.append({"role": msg.role, "content": msg.content})
        return out

    def chat(self, request: ChatRequest) -> ChatResponse:
        body: dict[str, Any] = {"model": self.model, "messages": self._messages(request),
                                self.max_field: request.max_tokens}
        if request.tools:
            body["tools"] = [{"type": "function", "function": {"name": t.name, "description": t.description,
                                                               "parameters": t.parameters}} for t in request.tools]
            body["tool_choice"] = "auto"
        elif request.json_schema and self.structured:
            body["response_format"] = {"type": "json_schema",
                                       "json_schema": {"name": "output", "schema": request.json_schema, "strict": True}}
        data = post_json(self.client, f"{self.base}/chat/completions", body, self._headers(), self.retries)
        try:
            choice = data["choices"][0]
            msg = choice["message"]
        except (KeyError, IndexError) as e:
            raise ModelError(f"{self.conn.id}: malformed reply ({e})") from e
        calls: list[ToolCall] = []
        for c in msg.get("tool_calls") or []:
            fn = c.get("function") or {}
            if not fn.get("name"):
                continue
            raw = fn.get("arguments")
            if isinstance(raw, dict):
                args: Any = raw
            else:
                try:
                    args = json.loads(raw or "{}")
                except (TypeError, ValueError):
                    args = {"_invalid_json": str(raw)[:500]}
            calls.append(ToolCall(id=str(c.get("id") or f"call_{len(calls)}"), name=str(fn["name"]),
                                  arguments=args if isinstance(args, dict) else {}))
        finish = choice.get("finish_reason")
        stop: Stop = "tool" if calls else STOP_OPENAI.get(str(finish), "end")
        usage = data.get("usage") or {}
        return ChatResponse(text=str(msg.get("content") or ""), tool_calls=calls, stop=stop,
                            usage=Usage(input_tokens=int(usage.get("prompt_tokens") or 0),
                                        output_tokens=int(usage.get("completion_tokens") or 0)))

    def is_external(self) -> bool:
        return self.external

    def health(self) -> Health:
        if not self.base or not self.model:
            return Health(status="down", detail="base_url and model are required")
        try:
            r = self.client.get(f"{self.base}/models", headers=self._headers(), timeout=10)
            return Health(status="ok" if r.status_code < 400 else "degraded", detail=f"{self.base} | {self.model}")
        except httpx.HTTPError as e:
            return Health(status="down", detail=f"{type(e).__name__}")


# --- Ollama chat (the adapter class lives in models.py) ----------------------------------------------


def ollama_chat(client: httpx.Client, base: str, model: str, request: ChatRequest, retries: int = 2,
                num_ctx: int = 16384) -> ChatResponse:
    msgs: list[dict[str, Any]] = [{"role": "system", "content": request.system}] if request.system else []
    for msg in request.messages:
        if msg.role == "tool":
            msgs.append({"role": "tool", "content": msg.content, "tool_name": msg.tool_name or ""})
        elif msg.role == "assistant" and msg.tool_calls:
            msgs.append({"role": "assistant", "content": msg.content,
                         "tool_calls": [{"function": {"name": c.name, "arguments": c.arguments}} for c in msg.tool_calls]})
        else:
            msgs.append({"role": msg.role, "content": msg.content})
    body: dict[str, Any] = {"model": model, "messages": msgs, "stream": False,
                            "options": {"num_predict": request.max_tokens, "num_ctx": num_ctx}}
    if request.tools:
        body["tools"] = [{"type": "function", "function": {"name": t.name, "description": t.description,
                                                           "parameters": t.parameters}} for t in request.tools]
    elif request.json_schema:
        body["format"] = request.json_schema
    data = post_json(client, f"{base}/api/chat", body, retries=retries)
    reply: dict[str, Any] = data.get("message") or {}
    calls = [ToolCall(id=f"call_{i}", name=c["function"]["name"],
                      arguments=c["function"].get("arguments") if isinstance(c["function"].get("arguments"), dict) else {})
             for i, c in enumerate(reply.get("tool_calls") or [])]
    stop: Stop = "tool" if calls else ("length" if data.get("done_reason") == "length" else "end")
    if int(data.get("prompt_eval_count") or 0) >= num_ctx - 32:
        stop = "length"  # Ollama drops the start of an over-long prompt silently: never trust that reply
    return ChatResponse(text=str(reply.get("content") or ""), tool_calls=calls, stop=stop,
                        usage=Usage(input_tokens=int(data.get("prompt_eval_count") or 0),
                                    output_tokens=int(data.get("eval_count") or 0)))


# --- Anthropic Messages API (optional extra) ------------------------------------------------------


@register("anthropic_api")
class AnthropicApi(_ChatOnly):
    """Config: {model (required), [secret_ref: anthropic-api-key], [timeout: 120], [retries: 2], [strict_tools: true],
    [refusal_fallback: true]}. Needs `gatewright[anthropic]`. Always external, so always redacted."""

    def __init__(self, conn: m.Connection, client: Any = None) -> None:
        cfg = resolve_config(conn.config)
        self.conn = conn
        self.model = str(cfg.get("model") or "")  # the model is always named in the connection, never here
        self.strict = bool(cfg.get("strict_tools", True))
        self.fallback = bool(cfg.get("refusal_fallback", True))
        self._key = secret(cfg.get("secret_ref", "anthropic-api-key"))
        self._client = client
        self._timeout = float(cfg.get("timeout", 120))
        self._retries = int(cfg.get("retries", 2))

    def _sdk(self) -> Any:
        if self._client is None:
            try:
                import anthropic
            except ImportError as e:
                raise ModelError("anthropic_api needs the extra: pip install gatewright[anthropic]") from e
            self._client = anthropic.Anthropic(api_key=self._key, timeout=self._timeout, max_retries=self._retries)
        return self._client

    @staticmethod
    def _messages(req: ChatRequest) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for msg in req.messages:
            if msg.role == "tool":
                block = {"type": "tool_result", "tool_use_id": msg.tool_call_id, "content": msg.content,
                         "is_error": msg.is_error}
                if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list) \
                        and all(b.get("type") == "tool_result" for b in out[-1]["content"]):
                    out[-1]["content"].append(block)  # all results of one turn in one user message
                else:
                    out.append({"role": "user", "content": [block]})
            elif msg.role == "assistant":
                if msg.native is not None:
                    out.append({"role": "assistant", "content": msg.native})  # echoed unchanged (thinking blocks)
                else:
                    blocks: list[dict[str, Any]] = [{"type": "text", "text": msg.content}] if msg.content else []
                    blocks += [{"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments}
                               for c in msg.tool_calls]
                    out.append({"role": "assistant", "content": blocks or msg.content})
            else:
                out.append({"role": "user", "content": msg.content})
        return out

    def chat(self, request: ChatRequest) -> ChatResponse:
        try:
            import anthropic
        except ImportError as e:
            raise ModelError("anthropic_api needs the extra: pip install gatewright[anthropic]") from e

        params: dict[str, Any] = {"model": self.model, "max_tokens": request.max_tokens,
                                  "messages": self._messages(request)}
        if request.system:
            params["system"] = request.system
        output_config: dict[str, Any] = {}
        if request.tools:
            params["tools"] = [{"name": t.name, "description": t.description, "input_schema": t.parameters,
                                **({"strict": True} if self.strict else {})} for t in request.tools]
        elif request.json_schema:
            output_config["format"] = {"type": "json_schema", "schema": request.json_schema}
        if request.effort:
            output_config["effort"] = request.effort
        if output_config:
            params["output_config"] = output_config
        try:
            if self.fallback:  # a policy decline is retried server-side on a fallback model
                res = self._sdk().beta.messages.create(betas=["server-side-fallback-2026-07-01"], fallbacks="default",
                                                       **params)
            else:
                res = self._sdk().messages.create(**params)
        except anthropic.APIStatusError as e:
            raise ModelError(f"{self.conn.id}: HTTP {e.status_code} ({type(e).__name__})") from e
        except anthropic.APIConnectionError as e:
            raise ModelError(f"{self.conn.id}: {type(e).__name__}") from e
        blocks = list(res.content)
        cut = max((i for i, b in enumerate(blocks) if getattr(b, "type", "") == "fallback"), default=-1)
        text = "".join(b.text for b in blocks[cut + 1:] if getattr(b, "type", "") == "text")  # after any fallback
        calls = [ToolCall(id=b.id, name=b.name, arguments=dict(b.input or {}))
                 for b in res.content if getattr(b, "type", "") == "tool_use"]
        stop: Stop = STOP_ANTHROPIC.get(str(res.stop_reason), "end")
        return ChatResponse(text=text, tool_calls=calls, stop=stop, native=list(res.content),
                            usage=Usage(input_tokens=int(res.usage.input_tokens or 0),
                                        output_tokens=int(res.usage.output_tokens or 0)))

    def is_external(self) -> bool:
        return True

    def health(self) -> Health:
        if not self.model:
            return Health(status="down", detail="config.model is required")
        if not self._key:
            return Health(status="down", detail="no API key (secret_ref)")
        try:
            import anthropic  # noqa: F401
        except ImportError:
            return Health(status="down", detail="pip install gatewright[anthropic]")
        return Health(status="ok", detail="api key set")
