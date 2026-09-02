# Persona Enrichment Runtime

Persona Continuum now treats research access and local evidence processing as
separate, auditable inputs to the existing Persona compilation pipeline.

## Research capability

Every `AgentProbeResult` carries a `ResearchCapability` with an explicit
`verification_status`: `declared`, `verified`, `unknown`, or `unavailable`.
An unreported capability is not treated as unavailable. A READY local CLI is
eligible for an isolated behavioral probe; API runtimes remain strict and do
not receive a CLI probe. Gemini's native route is a declaration until the
first real probe succeeds. Codex help/doctor output is only a hint.

The backend resolver chooses, in order:

1. a cached verified native backend for the exact Agent version, Model and
   runtime source;
2. a behavioral `AgenticCliResearchBackend` for an unknown/declared READY
   local CLI;
3. the selected adapter's explicit native web backend;
4. the configured `ResearchToolBroker`;
5. an explicitly configured MCP research backend.

The behavioral probe starts the selected Agent session, requests one real
OpenAI-hosted URL, and independently validates it with HTTP HEAD/GET. A model
claim without a reachable URL and non-empty body is never promoted to
`verified` or admitted as evidence. Results are persisted in
`research_capability_cache`, keyed by `agent_id`, `agent_version`, `model_id`
and `runtime_source`; a version change therefore requires a new probe.

Native results require a canonical URL, content and citation/source identity
before they can enter `EvidenceSource`.

## Enrichment input modes

`local_materials`, `web_research`, and `hybrid` are independent from
`PersonaType`. A public Persona may be upgraded from local files without web
capability. Web and hybrid modes require a resolved research backend; remote
providers still require explicit private-material consent.

## Child runs and evidence delta

An enrichment creates a new Persona Creation child run. The completed parent
remains terminal, its artifacts are copied into a new compilation task, and
the material snapshot is persisted before the worker starts. The run records
`parent_job_id`, base version, input material ids/count, new source ids and an
evidence delta. Duplicate-only input produces `NO_NEW_INFORMATION` and does
not create a profile version.

## Agent processing gate

Local files are data-local, not deterministic-only. Material Intelligence
performs parsing, segmentation, model classification, semantic relation,
fusion, indexing, gap analysis and the eight dimension extraction calls. A
material run cannot complete unless both material and dimension Agent calls
were executed (unless explicitly marked deterministic-only).

## Progress

Profile enrichment persists progress and events, including material stages,
Agent call counts, dimension progress and version transitions. The web UI polls
the 202-created `ProfileEnrichmentJob` until a terminal state and shows the
runtime, current stage, Agent statistics and `NO_NEW_INFORMATION` result.
