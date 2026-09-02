# Narrative Studio

Narrative Studio is the **author control layer** for long-form story production
(AI novels, micro dramas, series, interactive stories). It sits on top of the
existing Persona Continuum stack and adds nothing that duplicates it:

```
Narrative Studio      作者控制 / Canon / 剧集 / 伏笔 / 信息差
        │
Narrative Layer
        │
Parallel World        世界、因果、分支、模拟
        │
Scene / Room          人物真实互动（Room Protocol Engine）
        │
Persona               人格、记忆、情绪、欲望、关系
        │
Agent Runtime         CLI / API Agent、RuntimePool、Context Budget
```

- Persona (identity) != Agent Host != Model != Reasoning Effort != Room Session
  != World Actor != Narrative Role. Runtime/model/persona binding stays free.
- The engine is domain-agnostic: no example world (companies, markets,
  technologies) is ever auto-generated. All initial conditions come from the
  WorldSeed or Story Bible.

## Core concepts

| Concept | File |
| --- | --- |
| Domain models (project, bible, canon, episodes, scenes, clues…) | `domain/narrative.py` |
| Storage (`narrative_*` tables) | `narrative/repository.py`, `storage/migrations.py` |
| Knowledge Firewall (Story Truth / Character / Audience) | `narrative/knowledge_firewall.py` |
| Context Builder (task-scoped, budget aware) | `narrative/context_builder.py` |
| Continuity Auditor (BLOCKING/WARNING/INFO) | `narrative/continuity_auditor.py` |
| Screenwriter Stage (simulation → commercial script) | `narrative/screenwriter.py` |
| Production Package (AI video) | `narrative/production.py` |
| AI Writer's Room (Room Protocol Engine template) | `narrative/writer_room.py` |
| Application service + background jobs | `application/narrative_service.py` |

## Canon rule

Everything simulated is `noncanonical`. Only an explicit, audited, atomic
`commit_episode` promotes content to canon (see
[NARRATIVE_EPISODE_PIPELINE.md](NARRATIVE_EPISODE_PIPELINE.md)).

## Surfaces

- REST under `/api/narratives...` and `/api/narrative-jobs...` (`web/server.py`)
- WebSocket: `/api/narrative-jobs/{job_id}/ws`
- MCP tools: `narrative_*` (see [MCP_TOOLS.md](MCP_TOOLS.md))
- Web UI: 叙事创作 tab — creator workbench (作品设定 → 故事圣经 → 角色 → 全剧大纲 → 单集创作 → 连续性 → 制作). Runtime routing lives in ⚙ 创作模型. Information gaps / clues are under 连续性. See [NARRATIVE_STUDIO_FRONTEND_UX_ACCEPTANCE.md](reports/acceptance/NARRATIVE_STUDIO_FRONTEND_UX_ACCEPTANCE.md).
