"""The `gw` command line."""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path
from typing import Annotated, Any

import typer

from gw.errors import GwError

app = typer.Typer(help="Gatewright - AI PMO harness for capital project delivery.", no_args_is_help=True,
                  pretty_exceptions_enable=False)
standards = typer.Typer(help="Gate models: validate, publish, list.")
policy = typer.Typer(help="Policies: show, diff, set.")
harness = typer.Typer(help="Connections: add, test, list.")
forecast = typer.Typer(help="Prediction engine: run, calibrate, explain.")
semantic = typer.Typer(help="Semantic engine: retrain, queue, process.")
privacy = typer.Typer(help="Personal data: export, erase.")
app.add_typer(standards, name="standards")
app.add_typer(policy, name="policy")
app.add_typer(harness, name="harness")
app.add_typer(forecast, name="forecast")
app.add_typer(semantic, name="semantic")
app.add_typer(privacy, name="privacy")

DB = Annotated[str, typer.Option("--db", envvar="GW_DB", help="DuckDB file or SQLAlchemy URL")]


def _svc(db: str) -> Any:
    from gw.surfaces.service import Service

    return Service.open(db)


def _out(obj: Any) -> None:
    typer.echo(json.dumps(obj, indent=2, default=str, ensure_ascii=False))


@app.command()
def init(db: DB = "gw.duckdb", admin: str = "admin", email: str = "admin@example.com") -> None:
    """Create the database, install the shipped gate models and default policies, add a local admin."""
    from gw.policies.store import install_default
    from gw.record import models as m
    from gw.record.ids import new_id
    from gw.record.store import Store
    from gw.standards.gatemodels import install_shipped

    st = Store.open(db)
    install_shipped(st)
    install_default(st)
    if not st.find_one(m.User, short=admin):
        st.insert(m.User(id=new_id("User"), email=email, name=admin, short=admin,
                         roles=["admin", "portfolio_lead", "viewer"]))
    typer.echo(f"initialised {db}")


@app.command()
def demo(db: DB = "gw.duckdb", seed: int = 7, cases: str | None = None,
         today: str | None = None, data_dir: str = "demo-data", fresh: bool = True,
         portfolio: str = typer.Option("dc", help="dc (capital projects) or integration (M&A, task lists only)"),
         play: bool = typer.Option(True, help="run sync, signals and the morning agents after building")) -> None:
    """Build the synthetic DC portfolio and 24 months of history."""
    from gw.demo.generator import build
    from gw.demo.portfolio import DemoConfig
    from gw.record.store import Store

    if fresh and "://" not in db and Path(db).exists():
        Path(db).unlink()
        wal = Path(db + ".wal")
        if wal.exists():
            wal.unlink()
    st = Store.open(db)
    from gw.demo.generator import portfolio_module

    pf = portfolio_module(portfolio)
    chosen = tuple(c.strip() for c in cases.split(",") if c.strip()) if cases is not None else pf.CASES
    cfg = DemoConfig(seed=seed, cases=chosen, target_commitments=pf.TARGET_COMMITMENTS,
                     target_inbound=pf.TARGET_INBOUND, corpus_size=pf.CORPUS_SIZE, priors_size=pf.PRIORS_SIZE)
    res = build(st, date.fromisoformat(today) if today else None, cfg, data_dir, portfolio=portfolio)
    typer.echo(f"{res.projects} projects | {res.parties} parties | {res.commitments} commitments | "
               f"{res.inbound_items} inbound items | data in {res.data_dir}")
    if play:
        from gw.surfaces.service import Service

        out = Service(st).morning(trigger="demo")
        typer.echo(" | ".join(f"{k}: {v}" for k, v in out.items()))


@app.command("demo-remote")
def demo_remote(db: DB = "gw.duckdb", mail: str = "mail-live", docs: str = "docs-live", n_docs: int = 6,
                n_msgs: int = 10) -> None:
    """Seed the masked live account with synthetic documents and replies, then sync them back."""
    from gw.demo.remote import seed

    res = seed(_svc(db), mail, docs, n_docs, n_msgs)
    _out(res.__dict__)


@app.command()
def leakcheck(paths: list[Path] = typer.Argument(None), db: DB = "gw.duckdb") -> None:
    """Scan files for credentials and for the identifiers of configured live connections."""
    from gw.surfaces.leakcheck import scan

    hits = scan([Path(p) for p in paths] if paths else [Path(".")], db)
    for h in hits:
        typer.echo(f"{h.path}:{h.line}: {h.rule}")
    raise typer.Exit(1 if hits else 0)


@app.command("export-public")
def export_public(to: Path = typer.Option(..., "--to", help="a fresh, empty directory"),
                  manifest: Path = typer.Option(..., "--manifest", help="allow-list of paths to copy"),
                  terms: Path | None = typer.Option(None, "--terms", help="private topic terms, one per line"),
                  db: DB = "gw.duckdb") -> None:
    """Copy the allow-listed paths into a fresh directory; refuse (and delete it) on any leak finding."""
    from gw.surfaces.leakcheck import export

    hits = export(Path("."), to, manifest, db, terms)
    for h in hits:
        typer.echo(f"{h.path}:{h.line}: {h.rule}", err=True)
    if hits:
        typer.echo(f"refused: {len(hits)} findings; nothing exported", err=True)
        raise typer.Exit(1)
    typer.echo(f"exported to {to}")


@app.command()
def serve(db: DB = "gw.duckdb", host: str = "127.0.0.1", port: int = 8000,
          mcp_http: bool = typer.Option(False, "--mcp-http", help="serve MCP at /mcp instead of stdio"),
          scheduler: bool = True,
          forwarded_allow_ips: str = typer.Option("", envvar="GW_FORWARDED_ALLOW_IPS",
                                                  help="the reverse proxy's address(es) whose X-Forwarded-* "
                                                       "headers are trusted; empty trusts none")) -> None:
    """Run the web UI and REST API on :8000 (MCP over stdio: `gw mcp`)."""
    import uvicorn

    from gw.surfaces.api.app import create_app

    if host not in ("127.0.0.1", "localhost", "::1"):
        from gw.surfaces.auth import login_required

        if not login_required(_svc(db).store):
            typer.echo(f"refused: binding {host} in trust mode would let anyone act as any user. "
                       "Set a password first: gw user password <short>", err=True)
            raise typer.Exit(2)

    if forwarded_allow_ips.strip() == "*":
        typer.echo("refused: trust forwarded headers from the proxy's address only, never '*'", err=True)
        raise typer.Exit(2)
    uvicorn.run(create_app(db, scheduler=scheduler, mcp_http=mcp_http), host=host, port=port,
                proxy_headers=bool(forwarded_allow_ips.strip()), forwarded_allow_ips=forwarded_allow_ips.strip() or None)


@app.command()
def mcp(db: DB = "gw.duckdb", user: str | None = None) -> None:
    """Run the MCP server over stdio (the local user is the principal)."""
    from gw.surfaces.mcp.server import build_server

    build_server(db, user).run()


@app.command()
def sync(conn: Annotated[str | None, typer.Argument()] = None, db: DB = "gw.duckdb") -> None:
    """Run harness sync now for one connection or all."""
    s = _svc(db)
    _out(s.harness_sync(s.user(None), conn))


@app.command()
def score(gate: Annotated[str, typer.Option("--gate", help="PROJECT/gate, e.g. DC-01/fel3")], db: DB = "gw.duckdb") -> None:
    """Readiness and gaps for one gate instance."""
    s = _svc(db)
    project, _, g = gate.partition("/")
    res = s.gate_gaps(s.user(None), project, g or None)
    sc = s.gate_score(s.user(None), project, g or None)
    typer.echo(f"{sc['project']}/{sc['gate']} {sc['name']}: readiness {sc['readiness']:.1%} of {sc['threshold']:.0%} "
               f"-> {sc['recommendation'].upper()}")
    for gap in res["gaps"]:
        typer.echo(f"  {'M' if gap['mandatory'] else ' '} {gap['weight']:>4g}  {gap['commitment'] or '-':7} "
                   f"{gap['name'][:46]:46} {gap['status']:9} {gap['reason']}  -> {gap['lever_label']}")


@app.command()
def chase(db: DB = "gw.duckdb", dry_run: bool = typer.Option(True, "--dry-run/--send")) -> None:
    """Show (or run) what the chaser would send today."""
    s = _svc(db)
    plan = s.chase_plan() if dry_run else s.run_chaser(trigger="manual")
    _out(plan)


@app.command()
def ask(question: str, user: str = "aleks", db: DB = "gw.duckdb") -> None:
    """Ask the assistant a question about the record (needs policies.models.assistant.connection)."""
    s = _svc(db)
    res = s.ask(s.user(user), question)
    typer.echo(res["answer"])
    for p in res.get("proposals") or []:
        typer.echo(f"  proposed: {p['kind']} {p['target']} - {p['reason']} ({p.get('url') or ''})")
    if res.get("ai_generated"):
        from gw.governance.disclosure import readable_line

        typer.echo(readable_line(res["ai_generated"]))


@app.command()
def brief(user: str = "aleks", db: DB = "gw.duckdb") -> None:
    """Render today's brief to stdout."""
    s = _svc(db)
    typer.echo(s.brief_text(s.user(user)))


@app.command()
def audit(db: DB = "gw.duckdb", agent: str | None = None, entity: str | None = None,
          since: str = "7d") -> None:
    """Agent runs and events."""
    s = _svc(db)
    res = s.audit_query(s.user(None), entity=entity, agent=agent, since_days=int(since.rstrip("d")))
    for r in res["runs"]:
        typer.echo(f"{r['started_at'][:16]} {r['agent']:10} {r['outcome']:9} {r['id']}  {r['summary']}")
    typer.echo(f"{len(res['events'])} events")


@app.command()
def export(what: str = "events", fmt: str = typer.Option("parquet", "--format"), out: str = "", db: DB = "gw.duckdb") -> None:
    """Export events or signals."""
    import pandas as pd

    s = _svc(db)
    if what == "events":
        rows = s.store.events(limit=10_000_000)
    else:
        rows = [r.model_dump(mode="json") for r in s.store.latest_signals()]
    path = out or f"{what}.{fmt}"
    df = pd.DataFrame(rows)
    for col in df.columns:
        if df[col].map(lambda v: isinstance(v, (dict, list))).any():
            df[col] = df[col].map(json.dumps)
    df.to_parquet(path) if fmt == "parquet" else df.to_csv(path, index=False)
    typer.echo(f"{len(df)} rows -> {path}")


@standards.command("validate")
def standards_validate(files: list[Path]) -> None:
    import yaml

    from gw.standards.gatemodels import validate

    bad = False
    for f in files:
        rep = validate(yaml.safe_load(f.read_text(encoding="utf-8")) or {})
        for w in rep.warnings:
            typer.echo(f"{f}: warning: {w}")
        for p, msg in rep.errors:
            typer.echo(f"{f}: error at {p or '/'}: {msg}")
            bad = True
        if rep.ok:
            typer.echo(f"{f}: ok")
    raise typer.Exit(1 if bad else 0)


@standards.command("publish")
def standards_publish(file: Path, db: DB = "gw.duckdb", user: str | None = None) -> None:
    s = _svc(db)
    _out(s.standards_publish(s.user(user), file.read_text(encoding="utf-8")))


@standards.command("list")
def standards_list(db: DB = "gw.duckdb") -> None:
    for mdl in _svc(db).gatemodels():
        typer.echo(f"{mdl['id']:22} rev {mdl['rev']}  {mdl['gates']} gates  used by {mdl['used_by']}  {mdl['name']}")


@policy.command("show")
def policy_show(section: str | None = None, db: DB = "gw.duckdb") -> None:
    s = _svc(db)
    _out(s.policy_get(s.user(None), section))


@policy.command("diff")
def policy_diff(db: DB = "gw.duckdb") -> None:
    from gw.record import models as m

    revs = _svc(db).store.find(m.PolicyRevision, order_by="-rev", limit=1)
    typer.echo(revs[0].diff or "(first revision)" if revs else "no policies")


@policy.command("set")
def policy_set(file: Path, note: str = "", db: DB = "gw.duckdb", user: str | None = None) -> None:
    s = _svc(db)
    _out(s.policy_set(s.user(user), file.read_text(encoding="utf-8"), note))


@harness.command("add")
def harness_add(file: Path, db: DB = "gw.duckdb") -> None:
    s = _svc(db)
    for c in s.registry.add_file(file):
        typer.echo(f"added {c.id} ({c.contract} via {c.adapter})")


users_app = typer.Typer(help="Local users: add, password, token.")
app.add_typer(users_app, name="user")


@users_app.command("add")
def user_add(short: str, name: str, email: str, roles: str = typer.Option(..., help="comma-separated roles"),
             db: DB = "gw.duckdb") -> None:
    """Add a local user (no password: trust mode until any user has one)."""
    from gw.record import models as m
    from gw.record.ids import new_id

    s = _svc(db)
    s.store.insert(m.User(id=new_id("User"), email=email.lower(), name=name, short=short,
                          roles=[r.strip() for r in roles.split(",") if r.strip()]))
    typer.echo(f"added {short}")


@users_app.command("password")
def user_password(short: str, db: DB = "gw.duckdb") -> None:
    """Set a password (argon2). The first password switches the server into login mode."""
    from gw.surfaces.auth import hash_password

    s = _svc(db)
    u = s.user(short)
    pw = typer.prompt("new password", hide_input=True, confirmation_prompt=True)
    if len(pw) < 12:
        raise typer.BadParameter("use at least 12 characters")
    u.password_hash = hash_password(pw)
    s.store.update(u, actor="system:auth", kind="password_set")
    typer.echo(f"password set for {short}; the server now requires sign-in")


@users_app.command("token")
def user_token(short: str, db: DB = "gw.duckdb") -> None:
    """Mint an API bearer token (shown once, stored hashed)."""
    from gw.surfaces.auth import create_api_token

    s = _svc(db)
    typer.echo(create_api_token(s.store, s.user(short)))


@app.command("webhook")
def webhook_add(url: str, events: str = typer.Option(..., help="comma-separated: approval.requested, gate.at_risk, "
                                                        "escalation.l4, brief.ready, connection.degraded"),
                secret_ref: str = typer.Option(..., help="key name; the value is read from GW_SECRET_<NAME>"),
                db: DB = "gw.duckdb") -> None:
    """Subscribe a URL to outbound webhooks (HMAC-signed)."""
    from gw.surfaces.webhooks import subscribe

    _out(subscribe(_svc(db).store, url, [e.strip() for e in events.split(",") if e.strip()], secret_ref))


taxonomy_app = typer.Typer(help="Semantic taxonomy: show, set (every save is a revision).")
app.add_typer(taxonomy_app, name="taxonomy")


@taxonomy_app.command("show")
def taxonomy_show(db: DB = "gw.duckdb") -> None:
    _out(_svc(db).taxonomy_get())


@taxonomy_app.command("set")
def taxonomy_set(file: Path, note: str = "", db: DB = "gw.duckdb", user: str | None = None) -> None:
    s = _svc(db)
    _out(s.taxonomy_set(s.user(user), file.read_text(encoding="utf-8"), note))


@app.command("governance")
def governance_nightly(db: DB = "gw.duckdb") -> None:
    """Nightly governance now: automation readiness snapshot and promotion proposals."""
    _out(_svc(db).nightly_governance())


@app.command("report")
def report_cmd(period: str = typer.Argument("weekly", help="weekly | monthly"), db: DB = "gw.duckdb") -> None:
    """Render a report now (monthly runs only on the first business day unless --force)."""
    s = _svc(db)
    if period not in ("weekly", "monthly"):
        typer.echo("period must be weekly or monthly", err=True)
        raise typer.Exit(2)
    res = s.run_reporter(period, trigger="manual") if period == "weekly" else s.run_reporter_monthly_if_due()
    _out(res.outputs[0].payload if res is not None and res.outputs else {"rendered": False, "period": period})


@harness.command("assess")
def harness_assess(conn: str, db: DB = "gw.duckdb") -> None:
    """Is this dependency-carrying plan good enough to treat as a schedule? (Planner premium)"""
    from gw.harness.assess import assess_connection

    _out(assess_connection(_svc(db).registry, conn))


@harness.command("login")
def harness_login(conn: str, db: DB = "gw.duckdb") -> None:
    """Device-code sign-in for a delegated Graph connection (the token cache stays outside the repo)."""
    s = _svc(db)
    typer.echo(f"signed in as {s.harness_login(s.user(None), conn, show=typer.echo)}")


@harness.command("test")
def harness_test(conn: str, db: DB = "gw.duckdb") -> None:
    s = _svc(db)
    _out(s.harness_test(s.user(None), conn))


@harness.command("list")
def harness_list(db: DB = "gw.duckdb") -> None:
    s = _svc(db)
    for c in s.harness_list(s.user(None)):
        typer.echo(f"{c['id']:14} {c['contract']:16} {c['adapter']:16} {c['health']:9} {','.join(c['scopes'])}")


@forecast.command("run")
def forecast_run(db: DB = "gw.duckdb") -> None:
    _out(_svc(db).run_forecast(trigger="manual"))


@forecast.command("calibrate")
def forecast_calibrate(db: DB = "gw.duckdb") -> None:
    _out(_svc(db).forecast_calibrate())


@forecast.command("explain")
def forecast_explain(target: str, db: DB = "gw.duckdb") -> None:
    s = _svc(db)
    _out(s.forecast_explain(s.user(None), target))


@semantic.command("retrain")
def semantic_retrain(db: DB = "gw.duckdb") -> None:
    _out(_svc(db).semantic_retrain())


@semantic.command("queue")
def semantic_queue(db: DB = "gw.duckdb") -> None:
    s = _svc(db)
    for it in s.review_queue(s.user(None)):
        typer.echo(f"{it['id']}  {it['kind']:8} {it.get('summary', '')[:70]}")


@semantic.command("process")
def semantic_process(db: DB = "gw.duckdb") -> None:
    _out(_svc(db).process_inbound())


@privacy.command("export")
def privacy_export(email: str, db: DB = "gw.duckdb") -> None:
    _out(_svc(db).privacy_export(email))


@privacy.command("erase")
def privacy_erase(email: str, db: DB = "gw.duckdb") -> None:
    _out(_svc(db).privacy_erase(email))


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    try:
        app()
    except GwError as e:
        typer.echo(json.dumps(e.problem(), indent=2), err=True)
        sys.exit(2)


if __name__ == "__main__":
    main()
