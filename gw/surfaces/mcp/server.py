"""MCP server `gatewright` with the gw.* tools, resources and prompts.

Every tool is a thin wrapper over the
service layer; errors come back as problem+json in an isError result.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from functools import wraps
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from gw.errors import GwError
from gw.surfaces.service import Service


def build_server(db: str = "gw.duckdb", user_key: str | None = None, service: Service | None = None,
                 http: bool = False) -> MCPServer:
    """Build the MCP server; over stdio the local user (or `user_key`) is the principal."""
    svc = service or Service.open(db)
    server = MCPServer("gatewright", instructions=(
        "Gatewright governs programmes that run on gates. Read the record with gw.* tools; "
        "signals and forecasts are derived and advisory; people stamp decisions through approvals."))

    def principal() -> Any:
        """stdio: the local user. HTTP: the bearer token's user when the server is in login mode."""
        from gw.record import models as m
        from gw.surfaces import auth
        from gw.surfaces.principal import Unauthenticated, bearer_user

        uid = bearer_user.get()
        if uid:
            return svc.store.get(m.User, uid)
        if http and auth.login_required(svc.store):
            raise Unauthenticated("MCP over HTTP needs a bearer token in login mode")
        return svc.user(user_key)

    def tool(name: str, description: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
            @wraps(fn)
            def wrapped(*a: Any, **k: Any) -> Any:
                try:
                    return fn(*a, **k)
                except GwError as e:
                    raise ToolError(json.dumps(e.problem())) from e
            server.tool(name=name, description=description)(wrapped)
            return fn
        return deco

    @tool("gw.portfolio.list", "Projects with current gate, readiness, next milestone and signals.")
    def portfolio_list(zone: str | None = None, gate: str | None = None, at_risk_only: bool = False) -> list[dict[str, Any]]:
        return svc.portfolio_list(principal(), zone, gate, at_risk_only)

    @tool("gw.project.get", "One project with gate instances, packages and open commitments.")
    def project_get(code: str) -> dict[str, Any]:
        return svc.project_get(principal(), code)

    @tool("gw.gate.score", "Readiness, recommendation and gap list for a gate instance.")
    def gate_score(project: str, gate: str | None = None) -> dict[str, Any]:
        return svc.gate_score(principal(), project, gate)

    @tool("gw.gate.gaps", "Gap list with reasons, levers and forecast.")
    def gate_gaps(project: str, gate: str | None = None) -> dict[str, Any]:
        return svc.gate_gaps(principal(), project, gate)

    @tool("gw.gate.pack", "Latest review pack draft (markdown).")
    def gate_pack(project: str, gate: str | None = None) -> dict[str, Any]:
        return svc.gate_pack(principal(), project, gate)

    @tool("gw.gate.decide", "Stamp a gate decision (pass, pass_with_actions, hold, stop); needs a decided_by role.")
    def gate_decide(project: str, gate: str, decision: str, note: str = "") -> dict[str, Any]:
        return svc.gate_decide(principal(), project, gate, decision, note)

    @tool("gw.commitment.list", "Commitments filtered by project, party, status or due window.")
    def commitment_list(project: str | None = None, party: str | None = None, status: str | None = None,
                        due_within_days: int | None = None) -> list[dict[str, Any]]:
        return svc.commitment_list(principal(), project, party, status, due_within_days)

    @tool("gw.commitment.chase", "Queue a chase or draft an escalation per the ladder.")
    def commitment_chase(commitment: str, level: int | None = None) -> dict[str, Any]:
        return svc.commitment_chase(principal(), commitment, level)

    @tool("gw.commitment.set_status", "Move a commitment through its state machine.")
    def commitment_set_status(commitment: str, status: str, new_due: str | None = None,
                              reason: str | None = None) -> dict[str, Any]:
        return svc.commitment_set_status(principal(), commitment, status, new_due, reason)

    @tool("gw.evidence.link", "Link evidence (hash computed through the document source).")
    def evidence_link(commitment: str, uri: str, title: str | None = None, connection: str | None = None) -> dict[str, Any]:
        return svc.evidence_link(principal(), commitment, uri, title, conn_id=connection)

    @tool("gw.evidence.accept", "Accept or reject evidence; needs a role from the deliverable's accept_roles.")
    def evidence_accept(evidence: str, accept: bool, reason: str | None = None) -> dict[str, Any]:
        return svc.evidence_accept(principal(), evidence, accept, reason)

    @tool("gw.chase.status", "Chase loop status: sent, confirmed, flagged, silent and next steps.")
    def chase_status(project: str | None = None, party: str | None = None) -> dict[str, Any]:
        return svc.chase_status(principal(), project, party)

    @tool("gw.signal.query", "Signal values with their inputs.")
    def signal_query(signal: str, scope: str, scope_id: str | None = None, history_days: int | None = None) -> list[dict[str, Any]]:
        return svc.signal_query(principal(), signal, scope, scope_id, history_days)

    @tool("gw.heatmap.get", "Exposure, load or trend matrix with labels and drivers.")
    def heatmap_get(mode: str = "exposure") -> dict[str, Any]:
        return svc.heatmap(principal(), mode)

    @tool("gw.forecast.slip", "Advisory P(slip), P50/P90 days and drivers.")
    def forecast_slip(project: str | None = None, commitment: str | None = None) -> list[dict[str, Any]]:
        return svc.forecast_slip(principal(), project, commitment)

    @tool("gw.forecast.gate", "Advisory P(pass on date) for a gate instance.")
    def forecast_gate(project: str, gate: str | None = None) -> dict[str, Any]:
        return svc.forecast_gate(principal(), project, gate)

    @tool("gw.forecast.whatif", "Re-score a target with levers from the fixed catalogue.")
    def forecast_whatif(target: str, levers: list[str]) -> dict[str, Any]:
        return svc.forecast_whatif(principal(), target, levers)

    @tool("gw.forecast.explain", "Top drivers with signed contributions.")
    def forecast_explain(target: str) -> dict[str, Any]:
        return svc.forecast_explain(principal(), target)

    @tool("gw.doc.classify", "Classify a document text (no side effect).")
    def doc_classify(text: str = "", name: str = "") -> dict[str, Any]:
        return svc.doc_classify(principal(), text, name)

    @tool("gw.reply.parse", "Parse a reply: intent, entities, candidate commitment (no side effect).")
    def reply_parse(text: str, thread_ref: str | None = None) -> dict[str, Any]:
        return svc.reply_parse(principal(), text, thread_ref)

    @tool("gw.search", "Search the record and federated retrieval sources; hits carry citations.")
    def search(query: str, k: int = 10, sources: list[str] | None = None) -> list[dict[str, Any]]:
        return svc.search(principal(), query, k, sources)

    @tool("gw.deps.propose", "Pending dependency proposals from the scout.")
    def deps_propose(project: str | None = None) -> list[dict[str, Any]]:
        return svc.deps(principal(), project, status="proposed")

    @tool("gw.deps.decide", "Accept or reject a dependency proposal.")
    def deps_decide(dependency: str, accept: bool, reason: str = "") -> dict[str, Any]:
        return svc.deps_decide(principal(), dependency, accept, reason)

    @tool("gw.tender.check", "Package completeness and bid comparison (commercial locked until technical sign-off).")
    def tender_check(package: str) -> dict[str, Any]:
        return svc.tender_check(principal(), package)

    @tool("gw.policy.get", "Current policies (rev).")
    def policy_get(section: str | None = None) -> dict[str, Any]:
        return svc.policy_get(principal(), section)

    @tool("gw.policy.set", "Validate and store a new policy revision.")
    def policy_set(yaml: str, note: str = "") -> dict[str, Any]:
        return svc.policy_set(principal(), yaml, note)

    @tool("gw.approval.list", "Pending approvals.")
    def approval_list(mine: bool = False) -> list[dict[str, Any]]:
        return svc.approval_list(principal(), mine)

    @tool("gw.approval.decide", "Approve, approve edited, or reject a drafted agent output.")
    def approval_decide(approval: str, decision: str, edited_output: str | None = None) -> dict[str, Any]:
        return svc.approval_decide(principal(), approval, decision, edited_output)

    @tool("gw.audit.query", "Events and agent runs for an entity, agent or user.")
    def audit_query(entity: str | None = None, agent: str | None = None, user: str | None = None,
                    since_days: int = 7) -> dict[str, Any]:
        return svc.audit_query(principal(), entity, agent, user, since_days)

    @tool("gw.harness.list", "Connections with health.")
    def harness_list() -> list[dict[str, Any]]:
        return svc.harness_list(principal())

    @tool("gw.harness.test", "Test one connection.")
    def harness_test(connection: str) -> dict[str, Any]:
        return svc.harness_test(principal(), connection)

    @tool("gw.harness.sync", "Sync one connection or all.")
    def harness_sync(connection: str | None = None) -> dict[str, Any]:
        return svc.harness_sync(principal(), connection)

    @tool("gw.ask", "Ask the Gatewright assistant; it answers from the record with citations and proposes, "
                    "never performs, next steps. Needs policies.models.assistant.connection.")
    def ask(question: str, conversation: str | None = None) -> dict[str, Any]:
        return svc.ask(principal(), question, conversation)

    @tool("gw.brief.today", "Today's morning brief.")
    def brief_today(user: str | None = None) -> dict[str, Any]:
        me = principal()
        if user and svc.user(user).id != me.id and "portfolio_lead" not in me.roles:
            raise ToolError("reading another user's brief needs portfolio_lead")
        return svc.brief(svc.user(user) if user else me)

    @server.resource("gw://policies", description="Current policies as YAML")
    def res_policies() -> str:
        from gw.policies.store import current_revision

        return current_revision(svc.store).yaml_text

    @server.resource("gw://lessons", description="The lessons library")
    def res_lessons() -> str:
        from gw.record import models as m

        return json.dumps([x.model_dump(mode="json") for x in svc.store.all(m.Lesson)], indent=1)

    @server.resource("gw://gatemodels/{model_id}", description="A gate model, latest revision, as YAML")
    def res_gatemodel(model_id: str) -> str:
        return str(next(x["yaml"] for x in svc.gatemodels() if x["id"] == model_id))

    @server.resource("gw://project/{code}/summary", description="Project summary")
    def res_project(code: str) -> str:
        return json.dumps(svc.project_get(principal(), code), default=str, indent=1)

    @server.prompt(name="gw.prompt.gate_review", description="Assemble a review discussion from a pack")
    def prompt_gate_review(project: str, gate: str | None = None) -> str:
        pack = svc.gate_pack(principal(), project, gate)
        return ("Run a gate review discussion from this pack. Stay within the facts and cite ids.\n\n" + str(pack["markdown"]))

    @server.prompt(name="gw.prompt.party_call", description="Talking points before calling a silent party")
    def prompt_party_call(party: str) -> str:
        st = svc.chase_status(principal(), None, party)
        return ("Prepare talking points for a call with this party: what is owed, since when, what depends on it. "
                "Be factual, no pressure tactics.\n\n" + json.dumps(st, default=str, indent=1))

    return server
