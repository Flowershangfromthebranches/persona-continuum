from __future__ import annotations

from starlette.testclient import TestClient

from persona_continuum.application.container import PersonaContinuum
from persona_continuum.config import Config
from persona_continuum.performance import default_tracer
from persona_continuum.web.server import create_web_app


def test_performance_summary_endpoint_is_read_only(tmp_path) -> None:
    cfg = Config(data_dir=tmp_path / "perf-web")
    app = PersonaContinuum(cfg, include_fake_agent=True)
    app.init()
    try:
        default_tracer().incr_global("model_list_count", 3)
        default_tracer().incr_global("physical_process_spawn_count", 1)
        client = TestClient(create_web_app(app))
        response = client.get("/api/performance/summary?limit=5")
        assert response.status_code == 200
        payload = response.json()["data"]
        assert payload["global_counters"]["model_list_count"] >= 3
        assert payload["global_counters"]["physical_process_spawn_count"] >= 1
        assert "runtime_pool" in payload and "scheduler" in payload
        assert "model_capability_cache" in payload
        assert "room_context" in payload
    finally:
        app.close()
