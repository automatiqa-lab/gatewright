# EU AI Act compliance - Gatewright

Not legal advice. This file records how Gatewright meets its transparency obligations, and why.
It is the project's defence file: every entry is dated, and its history is in git.

Contact for compliance and incident reports: aleks@automatiqa.io

## Role classification

**Gatewright is open-source software published by Automatiqa Lab, which is the provider of the
system when it generates content with a language model.** The lab places Gatewright on GitHub under
its own name. When an operator connects a model, the agents can word text - L3 escalation chases to
parties, the Gatekeeper's gate note, the morning brief summary, the assistant's answers - and the
semantic engine can extract fields from inbound replies, with a model chosen by whoever runs
Gatewright. The lab is therefore the provider of the system that generates that text and carries
Article 50(2) marking; whoever runs Gatewright for other people is a deployer
under Article 50(4). The lab does not train, fine-tune or distribute models and is not a provider of
a general-purpose AI model; Chapter V obligations stay with the model providers. The Article 2(12)
open-source exemption is not relied on, because it ends with any commercial use.

Recorded 2026-10-23. Confirmed against the working code 2026-10-25.

## Obligations that apply

| Obligation | Applies | How this project meets it |
|---|---|---|
| Art. 50(1) interaction disclosure | yes | The assistant sheet shows the session notice; chases, the emailed brief and approval cards carry the readable AI-generated line when a model wrote them |
| Art. 50(2) machine-readable marking | yes | Marking at the agent boundary: every agent output passes one constructor that sets the `ai_generated` envelope, carried into web sheets, REST, MCP tool results, outbound messages and the CLI (see the table below) |
| Art. 50(4) deployer disclosure | deployer-side | See "What remains the deployer's job" |
| Art. 4 AI literacy | yes | This file, the README section and the agent audit log |
| Art. 5 prohibited practices | screened | No profiling of people, no manipulation, no biometrics; Gatewright governs gates and commitments, not persons |
| Annex III high-risk | no | See screening below |

## Annex III screening

Gatewright governs programmes that run on gates - deliverables, commitments, evidence, forecasts.

- **Employment (point 4)** is the near miss. Commitments carry an owner and chasers reach people, but
  Gatewright allocates no work to individuals, monitors no individual's performance and takes no
  decision about a person. Response-rate and commitment-load signals are computed for parties -
  organisations and teams - and the exposure matrix scores projects, not people. A deployer who
  reconfigures signals to rate named individuals changes the purpose and must re-screen.
- **Critical infrastructure (point 2)**: Gatewright supports the delivery of capital projects, some of
  which become infrastructure. It is not a safety component in the management or operation of
  critical digital infrastructure, road traffic or utilities, and it never writes to a schedule.
- **Essential services and credit (point 5)**: no scoring of natural persons.
- No other Annex III area is touched.

Screened 2026-10-23. Re-run on any change of purpose or new modality.

## What this project does out of the box

Language models are off by default. The bundled synthetic demo runs on templates only, so it
produces no model-generated output and nothing is labelled. A model is used only after the operator
names a chat connection in `policies.models.drafting` (chaser L3 escalations, the Gatekeeper's note,
the brief summary) or `policies.models.assistant`, or configures an extraction model for the
semantic engine. Any provider works: a local Ollama model, an OpenAI-compatible endpoint or a hosted
API. Drafted text is accepted only if it keeps every fact of the template; otherwise the template is
used and the run records why.

When a model did write something, the marking is applied in code:

- An agent declares which payload keys a model wrote. The record's `AgentOutput` constructor turns
  that declaration into the `ai_generated` envelope, so no construction path skips it. Marking is
  idempotent and emits nothing when nothing was generated.
- Every agent action is an `AgentRun` in the event log with its input snapshot, output and cost.
- Text bound for an external model passes the redactor first; this is one of three hard limits in
  code, alongside never writing to a schedule tool and never messaging a sponsor or authority
  without a recorded approval.
- The model is never named in visible output.

| Surface | Marking |
|---|---|
| Record | `ai_generated` envelope on the agent output: scope (which fields), schema, system, review state, items drafted, timestamp |
| REST (JSON) | the envelope in the body and the header `X-AI-Generated: true` |
| Web sheets | a "drafted by AI" chip on each model-written item (chase drafts, the gate note, the brief summary, assistant answers, extracted reply fields), and `ai-generated`, `ai-scope`, `ai-schema` meta tags plus the header on that page |
| Assistant sheet | the Art. 50(1) session notice |
| MCP tool results | the envelope in the result payload, so a connected agent detects provenance without parsing prose |
| Outbound messages | the readable line appended at the harness boundary, before the adapter sends; the envelope travels on the message |
| Approval cards | the readable line on the card body |
| CLI | `gw ask` and `gw brief` print the readable line |
| Webhooks | event bodies carry ids and figures only, never generated text, so there is nothing to mark |
| Review packs and reports | written from templates, never by a model, so unmarked; Markdown front matter is ready for any export that does carry model text |

Review state moves from `unreviewed` to `accepted` when a person approves the draft. A person who
rewrites a draft takes editorial control and the label is removed (see carve-outs).

Marking schema: `automatiqa-disclosure/1`. Wording from `gw/governance/data/disclosure.yaml`
version 2, copied from the lab's canonical source; the wording is never a literal in code.

## Carve-outs, and why

- **Signals and forecasts** - readiness scores, exposure, slip and gate-pass probabilities - are
  computed numbers, not synthetic text. They carry full provenance in the event log (inputs, model,
  calibration, drivers) instead of an AI label.
- **Content a person writes or materially edits** before it is sent or published - an approved chase
  rewritten by the project manager, a gate decision note - is under that person's editorial control
  and is not labelled.
- **Retrieved source material** - schedule data, documents, replies from parties - is never labelled.

## What remains the deployer's job

- Keep the marking intact in every export and downstream system.
- Tell your own recipients when content they receive was AI-generated.
- Put authentication in front of any exposed instance; Gatewright holds programme and supplier data.

## Decision log

| Date | Decision | Why |
|---|---|---|
| 2026-10-23 | Classified as provider of the content-generating system once agents ship; Annex III screened negative | Agents draft text with a model the operator chooses; signals score parties and projects, never persons |
| 2026-10-23 | Signals and forecasts carry event-log provenance, not an AI label | They are computed numbers, not synthetic text; blanket labels mislead |
| 2026-10-23 | Disclosure middleware is wired before the first agent commit | Retrofitting a boundary leaves paths uncovered |
| 2026-10-25 | Working release: marking verified on every surface in the table above, with tests for each; models stay off by default | The code now generates text when an operator connects a model, so the defence file describes what ships, not the design |
| 2026-10-25 | Webhooks and template-only exports carry no label | They contain no model-written text; labelling them would misstate provenance |
