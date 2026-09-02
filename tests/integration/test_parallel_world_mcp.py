from __future__ import annotations

from persona_continuum.config import Config
from persona_continuum.mcp.server import (
    MCPApplicationContext,
    create_mcp_server,
    registered_tool_names,
)


def test_parallel_world_tools(tmp_path) -> None:
    # 1. Verify registered tool names
    names = registered_tool_names()
    required_tools = [
        "create_world",
        "start_simulation",
        "pause_world",
        "advance_time",
        "inspect_actor",
        "inspect_event",
        "query_causal_chain",
        "compare_branches",
        "replay_timeline",
        "evaluate_world",
    ]
    for req in required_tools:
        assert req in names

    # 2. Test server instantiation with context
    config = Config(data_dir=tmp_path / "mcp_world")
    context = MCPApplicationContext(config)
    server = create_mcp_server(context)
    assert server is not None

    app = context.app()

    # 3. Create world via app (actors are explicit; the engine has no default roster)
    w, b, s = app.worlds.create_world(
        description="A shipping magnate expands a trade route in 1125",
        title="MCP World Test",
        start_date="1125-03-01",
        initial_actors=["shen_guan", "quanzhou_bureau"],
    )
    assert w.id is not None
    assert b.id is not None

    # 4. Inspect Causal Chain & Replay
    chain = app.worlds.query_causal_chain(w.id, b.id, "divergence")
    assert isinstance(chain, list)

    replay = app.worlds.get_replay_trajectory(w.id, b.id)
    assert len(replay.steps) >= 1

    # 5. Organizations & Technologies: none exist unless the seed defines them
    orgs = app.worlds.list_organizations(w.id, b.id)
    assert orgs == {}

    techs = app.worlds.list_technologies(w.id, b.id)
    assert techs == {}

    # 6. Actors exist because the seed requested them explicitly
    actors = app.worlds.list_actors(w.id, b.id)
    assert {a.id for a in actors} == {"shen_guan", "quanzhou_bureau"}

    context.close()
