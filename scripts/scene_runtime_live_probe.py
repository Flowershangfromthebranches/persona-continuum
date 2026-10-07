"""Isolated real-host probe; uses synthetic identity/history, never live persona data."""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

from persona_continuum.application.container import PersonaContinuum
from persona_continuum.config import Config
from persona_continuum.room.models import ParticipantSlot, RoomMode


async def main() -> None:
    with tempfile.TemporaryDirectory(prefix="pc-scene-live-") as temp:
        app = PersonaContinuum(Config(data_dir=Path(temp)))
        app.init()
        room = None
        results = []
        try:
            app.personas.create_from_manifest(
                {
                    "id": "scene-live-probe",
                    "display_name": "小夏",
                    "persona_type": "fictional_or_synthetic_person",
                    "run_mode": "digital_continuation",
                }
            )
            room = app.orchestrator.create_room(
                title="Scene firewall synthetic acceptance",
                mode=RoomMode.MANUAL,
                participants=[
                    ParticipantSlot(
                        participant_id="p",
                        persona_id="scene-live-probe",
                        display_name="小夏",
                        runtime_selection="grok",
                        model_selection="grok-4.6",
                        reasoning_selection="high",
                        allow_agent_tools=False,
                        allow_mcp=False,
                    )
                ],
            )
            await app.orchestrator.start_room(room.id)
            state = app.orchestrator.get_room(room.id)
            assert state
            # Legacy transcript view exercises the same read firewall as migrated history.
            state.transcript = [
                {
                    "turn_id": f"legacy{i}",
                    "participant_id": "p",
                    "speaker_name": "小夏",
                    "content": "（轻轻抱住你）\n晚安，明早见。\n（转身望向窗外）",
                }
                for i in range(30)
            ]
            app.orchestrator._save_room_state(state, force=True)
            for message in [
                "第二天早上我醒了。早呀，你想先聊点什么？",
                "抱我一下，然后跟我说声晚安。",
            ]:
                events = [e async for e in app.orchestrator.step_turn(room.id, "p", message)]
                complete = next((e for e in events if e["event"] == "turn_completed"), {})
                turn = complete.get("turn", {})
                error = next((e.get("error") for e in events if e["event"] == "agent_error"), None)
                raw = str(turn.get("raw_content", ""))
                speech = str(turn.get("spoken_text", ""))
                results.append(
                    {
                        "input": message,
                        "raw_model_output": raw,
                        "speech": speech,
                        "actions": turn.get("actions", []),
                        "error": error,
                        "scene_time": turn.get("scene_time"),
                        "raw_has_stage_directions": "（" in raw,
                        "speech_has_stage_directions": "（" in speech,
                    }
                )
                print(json.dumps(results[-1], ensure_ascii=False), flush=True)
        finally:
            if room:
                await app.orchestrator.stop_room(room.id)
            app.close()
            out = Path("docs/reports/implementation/scene-runtime-live-probe.json")
            out.write_text(
                json.dumps(
                    {
                        "host": "grok",
                        "model": "grok-4.6",
                        "effort": "high",
                        "synthetic_identity": True,
                        "cases": results,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )


if __name__ == "__main__":
    asyncio.run(asyncio.wait_for(main(), timeout=240))
