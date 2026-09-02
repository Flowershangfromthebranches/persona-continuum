"""First-party Persona Continuum tools.

These inspect the persona's own memory, relationships and runtime state.  They
are granted to every participant that has agent tools enabled: they are the
persona's own self-model, not an external capability a room has to opt into.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from persona_continuum.room.tool_providers.base import (
    PROVIDER_READY,
    ProviderHealth,
    ToolExecutionContext,
    ToolProvider,
    openai_function_definition,
    tool_error,
)

if TYPE_CHECKING:
    from persona_continuum.application.container import PersonaContinuum

BUILTIN_TOOL_NAMES: tuple[str, ...] = (
    "persona_search_memories",
    "persona_get_relationship",
    "persona_get_runtime_state",
)


class PersonaBuiltinToolProvider(ToolProvider):
    provider_id = "persona_builtin"
    display_name = "Persona Continuum built-in"
    builtin = True

    def __init__(self, continuum: PersonaContinuum) -> None:
        self.continuum = continuum

    def is_available(self) -> bool:
        return self.continuum is not None

    async def list_tools(self) -> list[str]:
        return list(BUILTIN_TOOL_NAMES)

    @staticmethod
    def static_definitions() -> list[dict[str, Any]]:
        """Synchronous view of the built-in definitions.

        The built-in set is fixed, so callers that cannot await (legacy
        compatibility, static introspection) may read it directly.
        """

        return [
            openai_function_definition(
                "persona_search_memories",
                "Search historical memories and past events for a persona.",
                {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Search query keywords or phrase.",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Maximum number of memories to return (default 5).",
                            "default": 5,
                        },
                    },
                    "required": ["query"],
                },
            ),
            openai_function_definition(
                "persona_get_relationship",
                "Inspect relationship and familiarity with a counterpart.",
                {
                    "type": "object",
                    "properties": {
                        "counterpart": {
                            "type": "string",
                            "description": "Name or ID of the counterpart person or entity.",
                        }
                    },
                    "required": ["counterpart"],
                },
            ),
            openai_function_definition(
                "persona_get_runtime_state",
                "Inspect current internal emotions, active needs, and goals.",
                {
                    "type": "object",
                    "properties": {
                        "branch_id": {
                            "type": "string",
                            "description": "Branch ID (default 'main').",
                            "default": "main",
                        }
                    },
                },
            ),
        ]

    async def get_tool_definitions(self) -> list[dict[str, Any]]:
        return self.static_definitions()

    def can_handle(self, tool_name: str) -> bool:
        return tool_name in BUILTIN_TOOL_NAMES

    async def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> Any:
        branch_id = str(context.branch_id or "main")
        if tool_name == "persona_search_memories":
            return self._search_memories(arguments, branch_id, context.persona_id)
        if tool_name == "persona_get_relationship":
            return self._get_relationship(arguments, branch_id, context.persona_id)
        if tool_name == "persona_get_runtime_state":
            return self._get_runtime_state(arguments, branch_id, context.persona_id)
        return tool_error(
            "tool_not_available",
            tool=tool_name,
            message=f"Unknown built-in tool: {tool_name}",
            provider=self.provider_id,
        )

    def _search_memories(
        self, arguments: dict[str, Any], branch_id: str, persona_id: str
    ) -> dict[str, Any]:
        query = str(arguments.get("query", "")).strip()
        if not query:
            return tool_error(
                "invalid_tool_arguments",
                tool="persona_search_memories",
                message="Argument 'query' is required.",
                provider=self.provider_id,
            )
        try:
            limit = max(1, min(50, int(arguments.get("limit", 5) or 5)))
        except (TypeError, ValueError):
            return tool_error(
                "invalid_tool_arguments",
                tool="persona_search_memories",
                message="Argument 'limit' must be an integer.",
                provider=self.provider_id,
            )
        memories = self.continuum.memories.search_memories(
            persona_id=persona_id,
            query=query,
            limit=limit,
            branch_id=branch_id,
        )
        return {
            "ok": True,
            "status": "success",
            "count": len(memories),
            "memories": [
                {
                    "id": m.id,
                    "content": m.content,
                    "type": m.type.value,
                    "source_kind": m.source_kind,
                    "importance": m.importance,
                }
                for m in memories
            ],
        }

    def _get_relationship(
        self, arguments: dict[str, Any], branch_id: str, persona_id: str
    ) -> dict[str, Any]:
        counterpart = str(arguments.get("counterpart", "user") or "user").strip()
        if not counterpart:
            return tool_error(
                "invalid_tool_arguments",
                tool="persona_get_relationship",
                message="Argument 'counterpart' is required.",
                provider=self.provider_id,
            )
        rel = self.continuum.relationships.get_relationship(
            persona_id, counterpart, branch_id=branch_id
        )
        data = rel.model_dump(mode="json")
        data.update({"ok": True, "status": "success"})
        return data

    def _get_runtime_state(
        self, arguments: dict[str, Any], branch_id: str, persona_id: str
    ) -> dict[str, Any]:
        target_branch = str(arguments.get("branch_id") or branch_id or "main")
        data = dict(self.continuum.runtime_state(persona_id, target_branch))
        data.update({"ok": True, "status": "success"})
        return data

    async def health(self) -> ProviderHealth:
        return ProviderHealth(
            provider_id=self.provider_id,
            display_name=self.display_name,
            status=PROVIDER_READY if self.is_available() else "unavailable",
            available=self.is_available(),
            healthy=self.is_available(),
            tool_count=len(BUILTIN_TOOL_NAMES),
            tools=list(BUILTIN_TOOL_NAMES),
        )

    async def shutdown(self) -> None:
        return None
