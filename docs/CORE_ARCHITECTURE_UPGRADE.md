# Persona Continuum Core Architecture Upgrade

## Parallel World

Autonomous decisions now follow one enforceable path:

`Actor -> ActorRuntime -> Persona/Organization Binding -> WorldContextBuilder -> AgentAdapter -> LLM reasoning -> ActionProposal -> ActionResolver -> TimelineEvent -> WorldState/Memory/CausalGraph`

`ActorDecisionEngine` is only a compatibility facade over `ActorRuntime`; it contains no actor-ID branches. Rules remain valid as feasibility constraints and pacing inputs, but cannot produce an actor's decision. If no ready Agent Adapter can return a valid proposal, the simulation blocks instead of silently advancing an empty clock step.

Actor types are `persona_actor`, `organization_actor`, and `environment_actor`. Legacy enum values remain readable so persisted worlds continue to load. Persona actors can bind a Persona manifest and an organization profile at the same time. Organization actors bind resources, budget, departments, technology, strategy, and culture from branch state.

`WorldContextBuilder` filters future memories and future events before building the prompt. The prompt includes current time, known and forbidden future information, actor/world state, goals, resources, relationships, projects, economy, and recent branch events.

The LLM returns `ActionProposal`. A proposal cannot mutate state. `ActionResolver` checks budget, talent, technology maturity, relationship requirements, and deadlines, then returns `success`, `failure`, or `partial_success`. Only resolved actions generate timeline events and world mutations.

World experience, belief-change, and relationship-change memories are stored per branch. Forking copies the source branch's memory boundary, after which new experience remains isolated.

## Autonomous Room

Room mode is either `manual` or `autonomous`.

Autonomous flow:

`Host Agent(open) -> DiscussionDirector -> selected Persona Agent -> response -> DiscussionDirector -> ... -> Host Agent(steer) -> ... -> Host Agent(summary)`

The Discussion Director uses topic/expertise relevance, direct references, relationship strength, speaking history, and cooldown pressure. It does not use random rotation. Host opening, steering, and summary text is generated through an Agent Adapter session.

Room creation accepts `request_id`. Reusing it returns the existing room. The REST create call returns the room immediately and schedules initialization. Progress is persisted and broadcast as `room_initialization_progress`: `initializing_room`, `loading_personas`, `loading_memories`, `connecting_agents`, and `ready`.

## Credential System

Cloud/API credentials are owned by `CredentialManager`. AES-256-GCM encrypts the API key and custom headers at rest with a local mode-0600 key. Runtime resolution decrypts into a short-lived in-memory value for one request. Provider responses expose only a masked hint and header names.

Supported providers are OpenAI, OpenAI Compatible, Anthropic, Google, DeepSeek, Grok, and Custom Endpoint. API adapters receive a CredentialManager and credential ID; they do not read databases, configuration files, `.env` files, or environment variables directly. Legacy CLI environment resolution is centralized in the credential module for compatibility.

## Database Changes

- `credentials`: provider, base URL, authenticated encrypted secret blob, timestamps.
- `room_create_requests`: unique request ID to room mapping for idempotency.
- Existing `rooms.state_json` adds room mode, host, initialization stage/progress, and retained progress events.
- Existing branch-scoped `world_memories` uses additional `belief_change` and `relationship_change` categories.

## API Changes

- `POST /api/rooms`: adds `mode`, `request_id`, `initialize_async`, and `host_participant_id`; returns immediately.
- `POST /api/rooms/{room_id}/autonomous`: runs a bounded autonomous discussion.
- Room WebSocket action `autonomous`: starts the autonomous loop and streams Host/Director/Agent events.
- `POST /api/auth-profiles`: accepts `api_key` and the expanded provider types; the returned object never includes the key.
- `POST /api/providers/{id}/test`: returns `status`, `latency`, `models`, and `error`.
- Existing Room, World, Tavern, Timeline, Branch, Replay, Causal Graph, and manual control APIs remain available.
