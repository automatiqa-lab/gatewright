# Gatewright - AI PMO harness for capital project delivery

Gatewright is an open-source PMO harness for programmes that run on gates -
capital projects first, and equally M&A integrations, transformation and TOM
roll-outs, product and ERP programmes. It connects your schedule tool, planner,
mailbox and document store, scores each gate against a configurable gate model,
runs chasers and forecasts, and exposes everything as MCP tools. Every agent
action sits on an auditable record; people stamp the decisions.

Not related to OPA Gatekeeper (Kubernetes) or macOS Gatekeeper.

Part of [Automatiqa Lab](https://www.automatiqa.io). Project page: [automatiqa.io/gatewright](https://www.automatiqa.io/gatewright/).

![Gatewright system schematic: schedule, plan, document, message, cost and calendar connections are read through the harness into one record; gate models are data that set the readiness rules; semantic, retrieval and forecast engines hang off the record and signals are derived from it; six bounded agents work under an autonomy ladder with an approval gate and three hard limits in code; people stamp the decisions, chasers go to parties, and every surface reads one service layer](docs/gatewright-how-it-works-aleks-sidorecs.png)

*Figure 1 - How Gatewright works. By Aleks Sidorecs.*

## The rule it enforces

A project advances on evidence, not on the strength of its sponsor.

## How it works

The numbers follow the callouts in Figure 1.

1. **Connections, read only.** The schedule tool (P6, MS Project, Smartsheet, XER files), the planner (Planner, Jira, Asana), the document store, the mailbox and Teams, cost exports and the calendar are registered connections. Gatewright reads them; it never writes to a schedule.
2. **Gate models are data.** FEL, RIBA 2020 and a three-gate site model ship for capital projects; `integration-100-day` ships for M&A integrations. A company forks the library, not the code.
3. **The harness** holds the registry, nine capability contracts and connection health, and syncs each connection on its own cadence.
4. **One record, derived signals.** Gates, commitments, parties and evidence live on one record with an event log. Every indicator - readiness, float erosion, response rate, exposure - is computed from it; nobody types a RAG status.
5. **Three engines.** The semantic engine classifies replies and documents and links evidence, retrieval indexes the record without storing document bodies, and the forecast engine scores slip and gate-pass probability, calibrated, with its drivers shown.
6. **Auspex**, the Lab's prediction harness, is a candidate model provider for the forecast engine (open question 1).
7. **Six bounded agents** - chaser, gatekeeper, scout, tender, brief, reporter. One job, one input snapshot, one output type, one autonomy level, one audit record each.
8. **Autonomy is earned.** Agents observe, propose, draft or act; a person approves at the gate until the automation readiness score - the share of outputs approved unchanged over 30 days - earns the next step.
9. **Three hard limits, in code, not policy:** never write to a schedule tool; never message a sponsor or authority without a recorded approval; never send text to an external model without the redactor.
10. **People stamp.** Pass, hold and escalate are decisions by named people with roles, recorded as events.

A. **Surfaces.** Web sheets, the morning brief, MCP tools (`gw.*`), REST, CLI, webhooks and Teams cards all call one service layer, so a Claude or Copilot session sees the same record under the same approvals.

## What it does

| Capability | Sheet | Module |
|---|---|---|
| Portfolio war room with leading indicators | GW-100 | signals, record |
| Exposure and commitment-load heat maps | GW-110 | signals |
| Gate readiness scored against a configurable gate model | GW-200 | standards, gatekeeper agent |
| Gate model editor (FEL, RIBA, FEP/BEE, PRINCE2, custom) | GW-800 | standards |
| Closed chaser loop with structured replies and an approved escalation ladder | GW-300 | chaser agent, semantic engine |
| Inbox intelligence: classify replies and documents, link evidence | GW-310 | semantic engine, retrieval |
| Interdependency proposals across projects | GW-400 | scout agent |
| Slip and gate-pass forecasts with drivers, calibration and what-if | GW-500 | prediction engine |
| Connection harness with capability contracts | GW-600 | harness |
| Policies: cadences, ladders, autonomy, thresholds | GW-700 | policies, governance |
| Morning brief | GW-010 | brief agent |
| Tender package check, technical before commercial | GW-320 | tender agent |

## What it is not

Not a scheduling tool (schedules are read through P6, MS Project, Smartsheet,
XER/MSPDI files), not a document store (evidence is a link and a hash), not an
autonomous PMO (agents observe, propose, draft or act within policy; three hard
limits are fixed in code).

## Beyond capital projects

Gate models are data: FEL, RIBA 2020 and a three-gate site model ship for
capital projects; `integration-100-day` ships for M&A integrations run from
Planner or Jira. The chaser, readiness scoring, inbox intelligence and forecast
transfer unchanged to any gated programme. Schedule signals (float erosion,
DCMA 14, critical path) need a schedule network and are simply absent for a
programme that runs on a task list - Gatewright never fabricates them.

## Status

In design, about 40%: specification rev B (23 Oct 2026) is complete, with schemas,
reference gate models and a clickable mock-up of every sheet. Code lands here
milestone by milestone. Built in public.

## Related Automatiqa Lab projects

- [Auspex](https://github.com/automatiqa-lab/auspex) - the Lab's prediction harness, a candidate model provider for the forecast engine
- [Orchestriq Core](https://github.com/automatiqa-lab/orchestriq) - the kernel design behind evals, approvals and earned autonomy
- [Ship-Doc](https://github.com/automatiqa-lab/ship-doc) - fixed rules decide, a named person approves, Outlook chases: the same loop for one shipment

## EU AI Act

Gatewright's agents draft text with a language model - chases, gap notes, briefs - so that output carries transparency marking under Article 50.

- Content a model wrote is marked; content a person wrote, and numbers from signals and forecasts, are not. Signals and forecasts carry their provenance in the event log instead.
- Marking travels in two channels: a line you can read, and metadata a machine can detect in every export and in every MCP tool result. The schema is `automatiqa-disclosure/1`.
- The model is never named in visible output. Article 50 requires disclosing that content is AI-generated, not which system produced it.
- No model-generated output ships yet. The disclosure layer is wired before the first agent commit, not after.

If you deploy Gatewright for other people in the EU you are a deployer: keep the marking intact and make sure recipients can recognise AI-generated content as such.

Full classification and reasoning: [COMPLIANCE.md](COMPLIANCE.md).

## Licence

MIT. See [LICENSE](LICENSE) and [NOTICE](NOTICE).

The argument these projects sit under: [The Intelligent Orchestration Manifesto](https://www.automatiqa.io/manifesto/).
