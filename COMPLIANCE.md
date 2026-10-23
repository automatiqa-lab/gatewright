# EU AI Act compliance - Gatewright

Not legal advice. This file records how Gatewright meets its transparency obligations, and why.
It is the project's defence file: every entry is dated, and its history is in git.

Contact for compliance and incident reports: aleks@automatiqa.io

## Role classification

**Gatewright is open-source software published by Automatiqa Lab, which will be its provider once it
generates content with a language model.** The lab places Gatewright on GitHub under its own name.
The planned agents draft text - chases to parties, gate gap notes, the morning brief, reports - with
a model chosen by whoever runs Gatewright, so the lab is the provider of the system that generates
that text and carries Article 50(2) marking; whoever runs Gatewright for other people is a deployer
under Article 50(4). The lab does not train, fine-tune or distribute models and is not a provider of
a general-purpose AI model; Chapter V obligations stay with the model providers. The Article 2(12)
open-source exemption is not relied on, because it ends with any commercial use.

Recorded 2026-10-23.

## Obligations that apply

| Obligation | Applies | How this project meets it |
|---|---|---|
| Art. 50(1) interaction disclosure | yes, once the agents ship | Chases and messages a party receives carry the readable AI-generated line when a model drafted them |
| Art. 50(2) machine-readable marking | yes | Disclosure middleware at the agent boundary: every AgentRun output passes one constructor that sets the `ai_generated` envelope, carried into web sheets, the brief, REST, MCP tool results, webhooks and exports |
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

At 40% - specification rev B, no code - no model-generated output ships. The design fixes the
architecture now:

- Every agent action is an `AgentRun` in the event log, written through one constructor, which
  carries the marking fields, so no path can bypass it.
- Text bound for an external model passes the redactor first; this is one of three hard limits in
  code, alongside never writing to a schedule tool and never messaging a sponsor or authority
  without a recorded approval.
- Wording comes from `disclosure.yaml`, copied from the lab's canonical source with the first agent
  commit.

Marking schema: `automatiqa-disclosure/1`, wording from `disclosure.yaml` version 1 (added with the
first agent commit).

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
