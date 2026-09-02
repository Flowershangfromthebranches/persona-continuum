# Multi-Agent Discussion Room Runtime

## Overview

The Persona Continuum Multi-Agent Room Runtime provides a local-first, highly concurrent, and decoupled environment for multi-persona discussions, debates, and collaborative simulations.

```
+-----------------------------------------------------------------------------------+
|                            MultiAgentOrchestrator                                 |
|                                                                                   |
|  +--------------------+     +---------------------+     +----------------------+  |
|  |   SpeakerDirector  |     |     RecallGate      |     |  RandomBindingResolver|  |
|  | (Turn Selection)   |     | (Dynamic Retrieval) |     |  (Adapter/Model Map) |  |
|  +--------------------+     +---------------------+     +----------------------+  |
|                                                                                   |
|  +--------------------+     +---------------------+     +----------------------+  |
|  |   PromptComposer   |     |  PersonaToolBroker  |     |   Database / State   |  |
|  | (Layered Assembly) |     |  (MCP / Local Tools)|     |   (Transcripts/FTS5) |  |
|  +--------------------+     +---------------------+     +----------------------+  |
+-----------------------------------------------------------------------------------+
```

## Core Architecture Principles

1. **4-Way Decoupling**:
   - **Persona Identity**: Manifest, compiled knowledge, affect temperament, boundaries, and episodic memories.
   - **Agent Host Runtime**: The execution adapter (`Codex`, `Cursor`, `Claude Code`, `Grok`, `Gemini`, `OpenCode`, etc.).
   - **Model Selection**: The concrete model invoked by the host (`gpt-5`, `claude-3-7-sonnet`, `gemini-2.5-pro`, `grok-4`).
   - **Reasoning Effort**: The reasoning budget (`none`, `low`, `medium`, `high`, `xhigh`, `max`).

2. **Recall Gate Before Model Call**:
   - Before invoking any agent model generation, the orchestrator triggers the `RecallGate`.
   - Analyzes user utterance and recent room dialogue for temporal and factual inquiries (e.g., *"以前 Musk 关于火星说过什么？"*).
   - Retrieves historical memories via SQLite FTS5.
   - Strict event sequence: `speaker_selected` -> `recall_started` -> `recall_completed` -> `agent_started` -> `agent_message_delta` -> `agent_completed` -> `persona_commit_started` -> `persona_commit_completed` -> `turn_completed`.

3. **Independent Native Sessions**:
   - Multiple participants running on the same Agent Host have isolated session IDs (`room_id + participant_id`).
   - Workspace paths and session caches never collide.

4. **Speaker Director Modes**:
   - `DIRECTOR` / `AI_DIRECTOR`: Natural flow scoring incorporating speaking weight, silence bonuses, mention bonuses, question targeting, and consecutive turn suppression.
   - `ROUND_ROBIN`: Deterministic cycling across participant slots.
   - `MANUAL`: Explicit user/director speaker override.

5. **Local Persistence & Resume**:
   - Room sessions, binding snapshots, and turns are persisted in SQLite `rooms` and `room_transcripts` tables.
   - When resumed, existing bindings and turn indexes are restored seamlessly.
