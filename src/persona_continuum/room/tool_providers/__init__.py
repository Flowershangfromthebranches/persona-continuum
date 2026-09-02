"""Room Tool Provider registry.

``tool_permissions`` (room policy) and ``tool_availability`` (runtime fact)
live here as separate concepts.  Only their intersection may be advertised to
a model or executed on its behalf.
"""

from persona_continuum.room.tool_providers.base import (
    INVALID_TOOL_ARGUMENTS,
    TOOL_ERROR_CODES,
    TOOL_EXECUTION_FAILED,
    TOOL_NOT_AVAILABLE,
    TOOL_PERMISSION_DENIED,
    TOOL_PROVIDER_UNHEALTHY,
    TOOL_TIMEOUT,
    ProviderHealth,
    ToolExecutionContext,
    ToolProvider,
    is_tool_error,
    openai_function_definition,
    sanitize_tool_name,
    tool_error,
)
from persona_continuum.room.tool_providers.broker import RoomToolBroker
from persona_continuum.room.tool_providers.persona_builtin import (
    BUILTIN_TOOL_NAMES,
    PersonaBuiltinToolProvider,
)

__all__ = [
    "BUILTIN_TOOL_NAMES",
    "INVALID_TOOL_ARGUMENTS",
    "PersonaBuiltinToolProvider",
    "ProviderHealth",
    "RoomToolBroker",
    "TOOL_ERROR_CODES",
    "TOOL_EXECUTION_FAILED",
    "TOOL_NOT_AVAILABLE",
    "TOOL_PERMISSION_DENIED",
    "TOOL_PROVIDER_UNHEALTHY",
    "TOOL_TIMEOUT",
    "ToolExecutionContext",
    "ToolProvider",
    "is_tool_error",
    "openai_function_definition",
    "sanitize_tool_name",
    "tool_error",
]
