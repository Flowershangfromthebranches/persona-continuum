"""Local-first performance instrumentation for long-running persona workflows.

The tracer is deliberately dependency-free and in-memory: recording a span is
a ``perf_counter`` read plus a dict update, so keeping it attached to every
Persona Creation, Room, and World phase costs microseconds against calls that
take seconds of model time.
"""

from persona_continuum.performance.capability_cache import (
    ModelCapabilityCache,
    ModelCapabilityCacheStats,
    capability_cache_key,
    default_model_capability_cache,
)
from persona_continuum.performance.research_cache import (
    ResearchQueryCache,
    ResearchSourceCache,
)
from persona_continuum.performance.runtime_pool import (
    AgentRuntimePool,
    ManagedRuntime,
    PoolAcquiredRuntime,
    default_runtime_pool,
)
from persona_continuum.performance.scheduler import (
    ExecutionClass,
    ExecutionScheduler,
    default_execution_scheduler,
)
from persona_continuum.performance.tracing import (
    PerformanceTracer,
    default_tracer,
    get_default_tracer,
)

__all__ = [
    "AgentRuntimePool",
    "ExecutionClass",
    "ExecutionScheduler",
    "ManagedRuntime",
    "ModelCapabilityCache",
    "ModelCapabilityCacheStats",
    "PerformanceTracer",
    "PoolAcquiredRuntime",
    "ResearchQueryCache",
    "ResearchSourceCache",
    "capability_cache_key",
    "default_execution_scheduler",
    "default_model_capability_cache",
    "default_runtime_pool",
    "default_tracer",
    "get_default_tracer",
]
