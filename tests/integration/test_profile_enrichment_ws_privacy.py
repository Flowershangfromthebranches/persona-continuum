from __future__ import annotations

import json

from starlette.testclient import TestClient

from persona_continuum.domain.profile import (
    ProfileEnrichmentJob,
    ProfileEnrichmentStatus,
    ProfileType,
)
from persona_continuum.web.server import create_web_app

PRIVATE_TEXT = "这是一段只应留在本地的私人聊天原文"


def test_profile_enrichment_ws_never_echoes_material_text(app) -> None:
    profile = app.profile_library.create_profile(
        profile_type=ProfileType.INSTITUTION,
        display_name="WS Privacy Target",
    )
    job = app.profile_library.save_enrichment_job(
        ProfileEnrichmentJob(
            id="ws_privacy_job",
            target_profile_id=profile.id,
            target_profile_type=ProfileType.INSTITUTION,
            status=ProfileEnrichmentStatus.COMPLETED,
            progress={
                "stage": "completed",
                "materials": [{"id": "m1", "text": PRIVATE_TEXT}],
            },
        )
    )

    with (
        TestClient(create_web_app(app)) as client,
        client.websocket_connect(f"/api/profile-enrichment/jobs/{job.id}/ws") as websocket,
    ):
        raw = websocket.receive_text()

    assert PRIVATE_TEXT not in raw
    payload = json.loads(raw)
    assert payload["job"]["progress"]["material_count"] == 1
    assert "materials" not in payload["job"]["progress"]
