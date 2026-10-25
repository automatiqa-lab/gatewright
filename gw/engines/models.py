"""ModelProvider adapters and the gateway that enforces hard limit 3.

: `is_external()` is checked in the gateway, not by
the caller, and external calls always pass through the redactor.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from gw.errors import Forbidden
from gw.harness.contracts import ChatMessage, ChatModel, ChatRequest, ChatResponse, Prediction
from gw.record import models as m

# --- redaction ---------------------------------------------------------------------

EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
PHONE = re.compile(r"(?<!\w)(?:\+\d{1,3}[\s-]?)?(?:\(?\d{2,4}\)?[\s-]?){2,4}\d{2,4}(?!\w)")
AMOUNT = re.compile(r"(?:(?:EUR|USD|GBP|€|\$)\s?\d[\d.,]*(?:\s?(?:k|m|million))?|\d[\d.,]*\s?(?:EUR|USD|GBP|€))", re.I)
ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?)?")
SIGNOFF = re.compile(r"(?im)^(?:best regards|kind regards|regards|met vriendelijke groet|saludos|cordialement|atenciosamente)[,.]?\s*\n.*", re.S)


@dataclass
class Redactor:
    """Replaces emails, phones, person names, amounts and signatures with placeholders.

    The mapping lives only on this object, which lives for one call.
    """

    names: list[str] = field(default_factory=list)
    mapping: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_record(cls, users: list[m.User], parties: list[m.Party]) -> Redactor:
        names = [u.name for u in users] + [c.name for p in parties for c in p.contacts]
        return cls(names=sorted({n for n in names if n and len(n) > 2}, key=len, reverse=True))

    def _sub(self, kind: str, value: str) -> str:
        for k, v in self.mapping.items():
            if v == value and k.startswith(f"<{kind}_"):
                return k
        n = sum(1 for k in self.mapping if k.startswith(f"<{kind}_")) + 1
        key = f"<{kind}_{n}>"
        self.mapping[key] = value
        return key

    def redact(self, text: str) -> str:
        text = SIGNOFF.sub(lambda mt: "\n" + self._sub("SIGNATURE", mt.group(0)), text)
        text = EMAIL.sub(lambda mt: self._sub("EMAIL", mt.group(0)), text)
        for name in self.names:
            parts = [re.escape(name)]
            if "." in name and " " in name:  # "M. de Vries" -> also "de Vries"
                parts.append(re.escape(name.split(" ", 1)[1]))
            for pat in parts:
                text = re.sub(rf"(?<!\w){pat}(?!\w)", lambda mt: self._sub("PERSON", mt.group(0)), text, flags=re.I)
        text = AMOUNT.sub(lambda mt: self._sub("AMOUNT", mt.group(0)), text)
        text = PHONE.sub(lambda mt: mt.group(0) if ISO_DATE.fullmatch(mt.group(0).strip()) or
                         sum(ch.isdigit() for ch in mt.group(0)) < 8 else self._sub("PHONE", mt.group(0)), text)
        return text

    def redact_obj(self, obj: Any) -> Any:
        """Redact every string inside a JSON-like value (tool-call arguments)."""
        if isinstance(obj, str):
            return self.redact(obj)
        if isinstance(obj, dict):
            return {k: self.redact_obj(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self.redact_obj(v) for v in obj]
        return obj

    def restore(self, obj: Any) -> Any:
        if isinstance(obj, str):
            for k, v in self.mapping.items():
                obj = obj.replace(k, v)
            return obj
        if isinstance(obj, dict):
            return {k: self.restore(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self.restore(v) for v in obj]
        return obj


class ModelGateway:
    """Every ModelProvider call goes through here. Hard limit 3 lives in this class."""

    def __init__(self, provider: Any, redactor: Redactor | None = None) -> None:
        self.provider = provider
        self.redactor = redactor
        self.tokens = 0
        self.prompt_hashes: list[str] = []

    def _external(self) -> bool:
        return bool(self.provider.is_external())

    def _guard(self) -> Redactor | None:
        if self._external():
            if self.redactor is None:
                raise Forbidden("external model call without the redactor", limit="redaction_required")
            return Redactor(names=self.redactor.names)  # fresh mapping per call
        return None

    def complete(self, prompt: str, schema: dict[str, Any] | None = None, max_tokens: int = 800) -> Any:
        red = self._guard()
        sent = red.redact(prompt) if red else prompt
        self.prompt_hashes.append(hashlib.sha256(sent.encode()).hexdigest()[:16])
        self.tokens += len(sent) // 4
        out = self.provider.complete(sent, schema, max_tokens)
        return red.restore(out) if red else out

    def embed(self, texts: list[str]) -> list[list[float]]:
        red = self._guard()
        sent = [red.redact(t) for t in texts] if red else texts
        return list(self.provider.embed(sent))

    def tabular_fit_predict(self, X_ctx: Any, y_ctx: Any, X_query: Any, task: str) -> Prediction:
        if self._external():
            raise Forbidden("tabular models run locally only in rev B", limit="redaction_required")
        result: Prediction = self.provider.tabular_fit_predict(X_ctx, y_ctx, X_query, task)
        return result

    # --- model-neutral chat ------------------------------------------------------

    def session(self, budget_tokens: int, tool_result_chars: int = 6000) -> ChatSession:
        """One conversation: one redaction mapping for all turns, so placeholders stay stable.
        `budget_tokens` <= 0 means no model use at all (policies.agents.budgets.tokens)."""
        if not isinstance(self.provider, ChatModel):
            raise Forbidden(f"{type(self.provider).__name__} is not a chat model", limit="scope")
        return ChatSession(self, self._guard(), budget_tokens, tool_result_chars)


class BudgetExceeded(RuntimeError):
    """The run's token budget (policies.agents.budgets.tokens) is spent."""


class ChatSession:
    """Hard limit 3 for chat: every outbound text of every turn passes the redactor when the provider is
    external; the reply's text and tool arguments are restored before Gatewright code sees them."""

    def __init__(self, gateway: ModelGateway, redactor: Redactor | None, budget_tokens: int,
                 tool_result_chars: int = 6000) -> None:
        self.gateway = gateway
        self.red = redactor
        self.budget = budget_tokens
        self.tool_chars = tool_result_chars
        self.used = 0

    def _out(self, text: str) -> str:
        return self.red.redact(text) if self.red else text

    def _message(self, mm: ChatMessage) -> ChatMessage:
        """Outbound form of one message: redacted text and tool arguments; tool results redacted
        before they are cut to size, so a cut can never expose part of a name."""
        if mm.native is not None:  # the provider's own content, built from redacted input
            return mm
        content = self._out(mm.content)
        if mm.role == "tool" and len(content) > self.tool_chars:
            content = content[:self.tool_chars] + " ... [truncated; narrow the query]"
        calls = [c.model_copy(update={"arguments": self.red.redact_obj(c.arguments)}) for c in mm.tool_calls]             if self.red else mm.tool_calls
        return mm.model_copy(update={"content": content, "tool_calls": calls})

    def chat(self, request: ChatRequest) -> ChatResponse:
        if self.budget <= 0 or self.used >= self.budget:
            raise BudgetExceeded(f"token budget {self.budget} spent" if self.budget > 0 else "no token budget")
        sent = request.model_copy(update={
            "system": self._out(request.system),
            "messages": [self._message(mm) for mm in request.messages],
            "max_tokens": max(64, min(request.max_tokens, self.budget - self.used))})
        self.gateway.prompt_hashes.append(
            hashlib.sha256("\n".join([sent.system] + [x.content for x in sent.messages]).encode()).hexdigest()[:16])
        res: ChatResponse = self.gateway.provider.chat(sent)
        spent = res.usage.input_tokens + res.usage.output_tokens
        self.used += spent
        self.gateway.tokens += spent
        if self.red:
            res = res.model_copy(update={
                "text": self.red.restore(res.text),
                "tool_calls": [c.model_copy(update={"arguments": self.red.restore(c.arguments)}) for c in res.tool_calls]})
        return res


def chat_gateway(store: Any, conn_id: str) -> ModelGateway:
    """The gateway for a configured chat connection, with the redactor built from the record."""
    from gw.harness.registry import Registry

    reg = getattr(store, "registry", None) or Registry(store)
    provider = reg.get(conn_id).adapter
    if not isinstance(provider, ChatModel):
        raise Forbidden(f"{conn_id} is not a chat model connection", limit="scope")
    return ModelGateway(provider, Redactor.from_record(store.all(m.User), store.all(m.Party)))


def as_array(vectors: list[list[float]]) -> np.ndarray:
    return np.asarray(vectors, dtype=np.float32)
