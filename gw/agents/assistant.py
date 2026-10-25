"""The assistant: a bounded tool-using agent over the record.

A person asks a question; a chat model (any connection named in policies.models.assistant) plans and
calls Gatewright tools, then answers with citations. The loop is provider-neutral.

Boundaries, enforced here and not left to the model:
- tools are read-only service calls made as the asking user, so the user's roles apply;
- the only "action" is `propose_action`, which records a proposal and a link to the sheet where a
  person acts. Nothing is sent, decided or changed by the assistant;
- step, tool-call and token budgets, a per-user hourly rate, bounded tool results;
- every run is an AgentRun with its tool calls, proposals and cost; the answer carries the Art. 50
  envelope because a model wrote it; external providers see redacted text only (hard limit 3).
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from gw.agents.base import AgentOutput, Snapshot
from gw.errors import GwError, Unprocessable
from gw.harness.contracts import ChatMessage, ChatRequest, ToolSpec
from gw.policies.store import Policies
from gw.record import models as m
from gw.record.store import Store

PROMPT = (Path(__file__).resolve().parent / "prompts" / "assistant.md").read_text(encoding="utf-8")
RESULT_CHARS = 6000
CITE = re.compile(r"\b(?:[A-Z]{1,4}-\d{2,5}|(?:C|G|P|DEP|AP|L|PT)-[0-9A-Z]{8,})\b")
PROPOSAL_KINDS = ["chase", "decide_gate", "review_dependency", "review_approval", "open"]


def _obj(props: dict[str, Any]) -> dict[str, Any]:
    """A strict JSON Schema object: every property required, nothing extra (nullable = optional)."""
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


def _opt(t: str) -> dict[str, Any]:
    return {"type": [t, "null"]}


class Tools:
    """The assistant's tool catalogue: read-only service calls as `user`, plus propose_action."""

    def __init__(self, svc: Any, user: m.User) -> None:
        from gw.engines.forecast.engine import WHATIF

        self.svc, self.user = svc, user
        self.proposals: list[dict[str, Any]] = []
        self.seen: list[str] = []  # every tool result, for the grounding check
        self.specs: list[ToolSpec] = [
            ToolSpec(name="portfolio", description="Projects with current gate, readiness, next milestone and at-risk flag.",
                     parameters=_obj({"at_risk_only": {"type": "boolean"}})),
            ToolSpec(name="project", description="One project: gate instances, packages, open commitments.",
                     parameters=_obj({"code": {"type": "string", "description": "project code, e.g. DC-01"}})),
            ToolSpec(name="gate", description="Readiness, recommendation and gaps (with reasons and levers) for a project's current gate.",
                     parameters=_obj({"project": {"type": "string"}})),
            ToolSpec(name="commitments", description="Commitments filtered by project, party, status or due window (max 40).",
                     parameters=_obj({"project": _opt("string"), "party": _opt("string"), "status": _opt("string"),
                                      "due_within_days": _opt("integer")})),
            ToolSpec(name="chase_status", description="Chase loop: confirmed, flagged, silent commitments and next steps.",
                     parameters=_obj({"project": _opt("string")})),
            ToolSpec(name="forecast", description="Advisory P(slip) and P50/P90 delay with drivers (max 30 rows).",
                     parameters=_obj({"project": _opt("string"), "commitment": _opt("string")})),
            ToolSpec(name="forecast_whatif", description="Re-score one commitment's slip probability with levers from the fixed catalogue.",
                     parameters=_obj({"commitment": {"type": "string"},
                                      "levers": {"type": "array", "items": {"type": "string", "enum": sorted(WHATIF)},
                                                 "maxItems": 4}})),
            ToolSpec(name="search", description="Search the record (commitments, decisions, risks, lessons, gate models); hits carry citations.",
                     parameters=_obj({"query": {"type": "string"}})),
            ToolSpec(name="dependencies", description="Dependency proposals from the scout awaiting a decision.",
                     parameters=_obj({"project": _opt("string")})),
            ToolSpec(name="approvals", description="Pending approvals (drafted escalations, review packs, proposals).",
                     parameters=_obj({})),
            ToolSpec(name="brief", description="Today's morning brief for the asking user: focus items and changes.",
                     parameters=_obj({})),
            ToolSpec(name="propose_action",
                     description="Propose a next step for a person to review on its sheet. Does not perform it.",
                     parameters=_obj({"kind": {"type": "string", "enum": PROPOSAL_KINDS},
                                      "target": {"type": "string", "description": "the code the step is about"},
                                      "reason": {"type": "string"}})),
        ]
        self._calls: dict[str, Callable[[dict[str, Any]], Any]] = {
            "portfolio": lambda a: svc.portfolio_list(user, None, None, bool(a.get("at_risk_only"))),
            "project": lambda a: svc.project_get(user, str(a["code"])),
            "gate": lambda a: {**svc.gate_score(user, str(a["project"])), **svc.gate_gaps(user, str(a["project"]))},
            "commitments": lambda a: svc.commitment_list(user, a.get("project"), a.get("party"), a.get("status"),
                                                         a.get("due_within_days"))[:40],
            "chase_status": lambda a: svc.chase_status(user, a.get("project")),
            "forecast": lambda a: svc.forecast_slip(user, a.get("project"), a.get("commitment"))[:30],
            "forecast_whatif": lambda a: svc.forecast_whatif(user, str(a["commitment"]), list(a.get("levers") or [])),
            "search": lambda a: svc.search(user, str(a["query"]), 8, None),
            "dependencies": lambda a: svc.deps(user, a.get("project"), status="proposed")[:20],
            "approvals": lambda a: svc.approval_list(user, False)[:20],
            "brief": lambda a: {k: v for k, v in svc.brief(user).items() if k in ("summary", "items", "changed")},
            "propose_action": self._propose,
        }

    def _propose(self, a: dict[str, Any]) -> dict[str, Any]:
        from gw.surfaces.web.links import Linker

        kind, target = str(a.get("kind")), str(a.get("target", "")).strip()
        if kind not in PROPOSAL_KINDS or not target:
            raise Unprocessable("propose_action needs a kind from the list and a target code")
        st = self.svc.store
        url = None
        c = st.find_one(m.Commitment, code=target)
        p = st.find_one(m.Project, code=target)
        if c is not None:
            url = f"/commitments/{c.code}"
        elif p is not None:
            url = f"/projects/{p.code}"
        elif target.startswith(("DEP-", "AP-")):
            url = Linker(st).url(target)
        url = url or {"review_dependency": "/deps", "review_approval": "/agents", "chase": "/chasers"}.get(kind)
        prop = {"kind": kind, "target": target, "reason": str(a.get("reason", ""))[:300], "url": url}
        self.proposals.append(prop)
        return {"proposed": True, "for_review_at": url, "note": "A person decides on that sheet; nothing was changed."}

    def call(self, name: str, args: dict[str, Any]) -> tuple[str, bool]:
        """Run one tool; returns (bounded JSON text, is_error). Arguments must match the tool's schema."""
        import jsonschema

        fn = self._calls.get(name)
        spec = next((s for s in self.specs if s.name == name), None)
        if fn is None or spec is None:
            return json.dumps({"error": f"unknown tool {name}; use only the listed tools"}), True
        try:
            jsonschema.validate(args or {}, spec.parameters)
        except jsonschema.ValidationError as e:
            return json.dumps({"error": f"invalid arguments for {name}: {e.message}",
                               "schema": spec.parameters}), True
        if any(isinstance(v, str) and len(v) > 500 for v in (args or {}).values()):
            return json.dumps({"error": "argument too long"}), True
        try:
            out = fn(args or {})
        except GwError as e:
            return json.dumps(e.problem(), default=str, ensure_ascii=False), True
        except Exception as e:  # a tool failure is an answer for the model, never the end of the loop
            return json.dumps({"error": f"{name} failed: {type(e).__name__}"}, ensure_ascii=False), True
        text = json.dumps(out, default=str, ensure_ascii=False)[:200_000]
        self.seen.append(text)
        return text, False  # the session redacts it, then cuts it to policy size


def ungrounded(answer: str, sources: list[str]) -> list[str]:
    """Codes and numbers in the answer that no tool result or the question contains (shown to the reader).

    A number counts as grounded when a source holds it, or holds its fraction (66% for 0.66)."""
    import math

    from gw.engines.drafting import CODE, FACT_TOKENS

    text = " ".join(sources)
    nums = [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", text)]
    out = [c for c in set(CODE.findall(answer)) if c not in text]
    rest = CODE.sub(" ", answer)  # a code's digits are not a separate number
    for pat in FACT_TOKENS[1:]:
        for tok in {t.strip() for t in pat.findall(rest)}:
            if not tok or tok in text:
                continue
            raw = tok.rstrip("%").strip().replace(",", "")
            try:
                val = float(raw)
            except ValueError:
                out.append(tok)
                continue
            if not any(math.isclose(val, n, rel_tol=0.006, abs_tol=0.051) or math.isclose(val, n * 100, abs_tol=0.6)
                       for n in nums):
                out.append(tok)
    return sorted(set(out))[:20]


class Assistant:
    """One question, one bounded tool loop, one Answer output."""

    name = "assistant"
    output_type = "Answer"

    def __init__(self, svc: Any, user: m.User, question: str, history: list[tuple[str, str]] | None = None,
                 conversation: str | None = None) -> None:
        self.svc, self.user = svc, user
        self.question = question.strip()
        self.history = history or []
        self.conversation = conversation
        self.summary = ""
        self.tokens_used = 0

    def snapshot(self, store: Store, policies: Policies, **kw: Any) -> Snapshot:
        from gw.engines.models import chat_gateway

        cfg = dict(policies.get("models.assistant", {}) or {})
        if not cfg.get("connection"):
            raise Unprocessable("the assistant needs policies.models.assistant.connection (a chat connection)")
        budget = int(policies.get("agents.budgets.tokens.assistant", 60000) or 0)
        session = chat_gateway(store, str(cfg["connection"])).session(
            budget_tokens=budget, tool_result_chars=int(cfg.get("tool_result_chars", RESULT_CHARS)))
        self.seconds_budget = float(cfg.get("max_seconds", 180)) + 30  # run_agent's own limit, past our deadline
        return Snapshot(seq=store.last_seq(), policies=policies, data={"cfg": cfg, "session": session})

    def run(self, snap: Snapshot) -> list[AgentOutput]:
        from gw.engines.models import BudgetExceeded

        cfg, session = snap.data["cfg"], snap.data["session"]
        tools = Tools(self.svc, self.user)
        max_steps, max_calls = int(cfg.get("max_steps", 8)), int(cfg.get("max_tool_calls", 16))
        messages: list[ChatMessage] = []
        for q, a in self.history[-4:]:
            messages += [ChatMessage(role="user", content=q), ChatMessage(role="assistant", content=a)]
        messages.append(ChatMessage(role="user", content=f"{self.user.name} ({', '.join(self.user.roles)}) asks: "
                                                        f"{self.question}"))
        trace: list[dict[str, Any]] = []
        answer, stop, calls_used = "", "end", 0
        model_text = ""
        deadline = time.monotonic() + float(cfg.get("max_seconds", 180))
        try:
            for _step in range(max_steps):
                if time.monotonic() > deadline:
                    stop = "time"
                    break
                res = session.chat(ChatRequest(system=PROMPT, messages=messages, tools=tools.specs,
                                               max_tokens=int(cfg.get("max_tokens", 2500)), effort=cfg.get("effort")))
                stop = res.stop
                if res.stop == "tool" and res.tool_calls:
                    messages.append(ChatMessage(role="assistant", content=res.text, tool_calls=res.tool_calls,
                                                native=res.native))
                    for call in res.tool_calls:
                        calls_used += 1
                        text, err = tools.call(call.name, call.arguments) if calls_used <= max_calls else \
                            (json.dumps({"error": "tool-call budget spent; answer now with what you have"}), True)
                        trace.append({"tool": call.name, "args": call.arguments, "ok": not err})
                        messages.append(ChatMessage(role="tool", tool_call_id=call.id, tool_name=call.name,
                                                    content=text, is_error=err))
                    continue
                answer = model_text = res.text.strip()
                break
            else:
                stop = "steps"
        except BudgetExceeded:
            stop = "budget"
        except Exception as e:  # provider down or refused by a hard limit: say so, never guess
            stop = f"error: {type(e).__name__}"
        if stop == "refused":
            answer = model_text = ""
            answer = "The model declined this request. Rephrase it, or use the sheets directly."
        elif not answer:
            answer = {"steps": "I stopped at the step limit before finishing. Narrow the question.",
                      "budget": "I stopped at the token budget for this question. Narrow the question.",
                      "end": "The model returned no answer. Rephrase the question, or use the sheets directly.",
                      "length": "The answer was cut off at the token limit. Narrow the question.",
                      "time": "I stopped at the time limit for one question. Narrow the question."}.get(
                stop, "The assistant is unavailable right now (the model connection failed). The sheets still work.")
        answer = answer.replace("—", " - ")
        unverified = ungrounded(model_text, tools.seen + [self.question]) if model_text else []
        self.tokens_used = session.used
        seen = sorted({x for t in trace for x in CITE.findall(json.dumps(t["args"]))} | set(CITE.findall(answer)))
        cites = [x for x in seen if x][:30] or [self.user.id]
        self.summary = f"{len(trace)} tool calls, {len(tools.proposals)} proposals, stop {stop}"
        written = bool(model_text)  # only the model's own text carries the AI label, not our fallback lines
        return [AgentOutput(output_type="Answer", title=self.question[:80], citations=cites,
                            generated=["answer"] if written else [],
                            payload={"question": self.question, "answer": answer, "user": self.user.id,
                                     "conversation": self.conversation, "tool_calls": trace,
                                     "proposals": tools.proposals, "stop": stop, "tokens": session.used,
                                     "unverified": unverified})]
