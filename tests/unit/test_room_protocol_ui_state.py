from __future__ import annotations

import json
import subprocess
from pathlib import Path

UI_STATE = (
    Path(__file__).parents[2]
    / "src"
    / "persona_continuum"
    / "web"
    / "static"
    / "room_ui_state.js"
)


def _evaluate(expression: str) -> object:
    script = (
        f"const ui = require({json.dumps(str(UI_STATE))});"
        f"process.stdout.write(JSON.stringify({expression}));"
    )
    completed = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def test_protocol_success_settles_busy_and_stops_http_watch() -> None:
    room = {
        "protocol": "expert_consultation",
        "status": "ready",
        "protocol_state": {"status": "success"},
    }
    encoded = json.dumps(room)
    assert _evaluate(f"ui.shouldSettleBusy({encoded})") is True
    assert _evaluate(f"ui.shouldStopWatch({encoded})") is True


def test_http_polling_keeps_running_until_protocol_terminal() -> None:
    running = {
        "protocol": "expert_consultation",
        "status": "discussing",
        "protocol_state": {"status": "running"},
    }
    partial = {
        "protocol": "expert_consultation",
        "status": "ready",
        "protocol_state": {"status": "partial_success"},
    }
    assert _evaluate(f"ui.shouldStopWatch({json.dumps(running)})") is False
    assert _evaluate(f"ui.shouldSettleBusy({json.dumps(running)})") is False
    assert _evaluate(f"ui.shouldStopWatch({json.dumps(partial)})") is True
    assert _evaluate(f"ui.shouldSettleBusy({json.dumps(partial)})") is True


def test_interrupted_model_call_settles_busy_banner() -> None:
    room = {
        "protocol": "free_discussion",
        "status": "ready",
        "metadata": {"model_call": {"status": "interrupted"}},
        "protocol_state": {"status": "pending"},
    }
    assert _evaluate(f"ui.shouldSettleBusy({json.dumps(room)})") is True


def test_terminal_protocol_stage_does_not_look_active_after_refresh() -> None:
    success = {"status": "success", "current_stage": "final"}
    failed = {"status": "failed", "current_stage": "cross_review"}
    running = {"status": "running", "current_stage": "host_analysis"}
    # Terminal success keeps the final answer visible instead of the idle
    # stage; cancelled/waiting_clarification fall back to waiting_user.
    assert _evaluate(f"ui.protocolStageForDisplay({json.dumps(success)})") == "final_response"
    partial = {"status": "partial_success", "current_stage": "final"}
    cancelled = {"status": "cancelled", "current_stage": "synthesis"}
    clarification = {"status": "waiting_clarification", "current_stage": "host_analysis"}
    assert _evaluate(f"ui.protocolStageForDisplay({json.dumps(partial)})") == "final_response"
    assert _evaluate(f"ui.protocolStageForDisplay({json.dumps(cancelled)})") == "waiting_user"
    assert (
        _evaluate(f"ui.protocolStageForDisplay({json.dumps(clarification)})") == "waiting_user"
    )
    assert _evaluate(f"ui.protocolStageForDisplay({json.dumps(failed)})") == "failed"
    assert _evaluate(f"ui.protocolStageForDisplay({json.dumps(running)})") == "host_analysis"


def test_waiting_clarification_is_terminal_for_polling() -> None:
    room = {
        "protocol": "expert_consultation",
        "status": "ready",
        "protocol_state": {"status": "waiting_clarification"},
    }
    encoded = json.dumps(room)
    assert _evaluate("ui.isProtocolTerminal('waiting_clarification')") is True
    assert _evaluate(f"ui.shouldSettleBusy({encoded})") is True
    assert _evaluate(f"ui.shouldStopWatch({encoded})") is True


def test_should_suggest_protocol_upgrade_is_structural() -> None:
    host_plus_members = {
        "protocol": "free_discussion",
        "status": "ready",
        "participants": [
            {"participant_id": "h", "role": "host", "enabled": True},
            {"participant_id": "m1", "role": "member", "enabled": True},
            {"participant_id": "m2", "role": "member", "enabled": True},
        ],
    }
    assert _evaluate(f"ui.shouldSuggestProtocolUpgrade({json.dumps(host_plus_members)})") is True

    protocol_room = {**host_plus_members, "protocol": "expert_consultation"}
    assert (
        _evaluate(f"ui.shouldSuggestProtocolUpgrade({json.dumps(protocol_room)})") is False
    )
    # §2.2: a room without any host still qualifies -- the upgrade flow lets
    # the user pick the host from the enabled quorum.
    no_host = {
        "protocol": "free_discussion",
        "participants": [
            {"participant_id": "m1", "role": "member", "enabled": True},
            {"participant_id": "m2", "role": "member", "enabled": True},
        ],
    }
    assert _evaluate(f"ui.shouldSuggestProtocolUpgrade({json.dumps(no_host)})") is True
    # A disabled host does not block the suggestion either.
    disabled_host = {
        "protocol": "free_discussion",
        "participants": [
            {"participant_id": "h", "role": "host", "enabled": False},
            {"participant_id": "m1", "role": "member", "enabled": True},
            {"participant_id": "m2", "role": "member", "enabled": True},
        ],
    }
    assert (
        _evaluate(f"ui.shouldSuggestProtocolUpgrade({json.dumps(disabled_host)})") is True
    )
    # Disabled members do not count toward the enabled quorum.
    only_host_enabled = {
        "protocol": "free_discussion",
        "participants": [
            {"participant_id": "h", "role": "host", "enabled": True},
            {"participant_id": "m1", "role": "member", "enabled": False},
        ],
    }
    assert (
        _evaluate(f"ui.shouldSuggestProtocolUpgrade({json.dumps(only_host_enabled)})") is False
    )
    # Single participant can never form a consultation panel.
    solo = {
        "protocol": "free_discussion",
        "participants": [{"participant_id": "h", "role": "host", "enabled": True}],
    }
    assert _evaluate(f"ui.shouldSuggestProtocolUpgrade({json.dumps(solo)})") is False
    assert _evaluate("ui.shouldSuggestProtocolUpgrade(null)") is False


def test_protocol_upgrade_role_mapping_defers_host_choice_without_host() -> None:
    with_host = {
        "protocol": "free_discussion",
        "participants": [
            {"participant_id": "h", "role": "host", "enabled": True},
            {"participant_id": "m1", "role": "member", "enabled": True},
            {"participant_id": "m2", "role": "member", "enabled": True},
        ],
    }
    plan = _evaluate(f"ui.protocolUpgradeRoleMapping({json.dumps(with_host)})")
    assert plan == {
        "hostId": "h",
        "needsHostSelection": False,
        "candidates": [],
        "mapping": {"h": "host", "m1": "expert", "m2": "expert"},
    }

    # Without a host the mapping stays provisional (all expert) and the
    # caller must resolve one of `candidates` into the single host.
    no_host = {
        "protocol": "free_discussion",
        "participants": [
            {"participant_id": "m1", "role": "member", "enabled": True},
            {"participant_id": "m2", "role": "member", "enabled": True},
        ],
    }
    plan = _evaluate(f"ui.protocolUpgradeRoleMapping({json.dumps(no_host)})")
    assert plan["hostId"] is None
    assert plan["needsHostSelection"] is True
    assert plan["candidates"] == ["m1", "m2"]
    assert plan["mapping"] == {"m1": "expert", "m2": "expert"}

    # Disabled participants never enter the mapping payload.
    mixed = {
        "protocol": "free_discussion",
        "participants": [
            {"participant_id": "h", "role": "host", "enabled": True},
            {"participant_id": "m1", "role": "member", "enabled": False},
            {"participant_id": "m2", "role": "member", "enabled": True},
        ],
    }
    plan = _evaluate(f"ui.protocolUpgradeRoleMapping({json.dumps(mixed)})")
    assert plan == {
        "hostId": "h",
        "needsHostSelection": False,
        "candidates": [],
        "mapping": {"h": "host", "m2": "expert"},
    }


def test_legacy_autonomous_requires_explicit_user_message() -> None:
    free_room = {
        "protocol": "free_discussion",
        "mode": "autonomous",
        "status": "ready",
    }
    protocol_room = {**free_room, "protocol": "expert_consultation"}
    encoded = json.dumps(free_room)
    assert (
        _evaluate(f"ui.shouldStartLegacyAutonomous({encoded}, false, 'room_open')")
        is False
    )
    assert (
        _evaluate(f"ui.shouldStartLegacyAutonomous({encoded}, false, 'reconnect')")
        is False
    )
    assert (
        _evaluate(f"ui.shouldStartLegacyAutonomous({encoded}, false, 'user_message')")
        is True
    )
    assert (
        _evaluate(
            "ui.shouldStartLegacyAutonomous("
            f"{json.dumps(protocol_room)}, false, 'user_message')"
        )
        is False
    )


def test_completed_room_cannot_cancel_and_hides_protocol_panel() -> None:
    room = {
        "protocol": "free_discussion",
        "status": "completed",
        "metadata": {"model_call": {"status": "completed"}},
        "protocol_state": {"status": "pending"},
    }
    encoded = json.dumps(room)
    assert _evaluate(f"ui.canCancelTurn({encoded})") is False
    assert _evaluate(f"ui.canResumeRoom({encoded})") is True
    assert _evaluate(f"ui.shouldShowProtocolPanel({encoded})") is False


def test_only_paused_or_completed_room_can_resume() -> None:
    assert _evaluate('ui.canResumeRoom({"status":"paused"})') is True
    assert _evaluate('ui.canResumeRoom({"status":"completed"})') is True
    assert _evaluate('ui.canResumeRoom({"status":"ready"})') is False
    assert _evaluate('ui.canResumeRoom({"status":"discussing"})') is False


def test_running_protocol_can_cancel_and_shows_protocol_panel() -> None:
    room = {
        "protocol": "expert_consultation",
        "status": "discussing",
        "protocol_state": {"status": "running"},
    }
    encoded = json.dumps(room)
    assert _evaluate(f"ui.canCancelTurn({encoded})") is True
    assert _evaluate(f"ui.shouldShowProtocolPanel({encoded})") is True


def test_completed_success_run_keeps_protocol_panel_visible() -> None:
    # §32: a terminal "success" run keeps its final answer on screen -- the
    # protocol panel must stay visible for the settled room.
    room = {
        "protocol": "expert_consultation",
        "status": "ready",
        "protocol_state": {"status": "success"},
    }
    encoded = json.dumps(room)
    assert _evaluate("ui.isProtocolTerminal('success')") is True
    assert _evaluate(f"ui.shouldShowProtocolPanel({encoded})") is True


def test_room_binding_edit_gate() -> None:
    assert _evaluate("ui.canEditRoomBindings(null)") is False
    assert _evaluate('ui.roomBindingsEditBlockReason(null)') == "no_room"

    settled = json.dumps({"status": "ready", "protocol_state": {"status": "pending"}})
    assert _evaluate(f"ui.canEditRoomBindings({settled})") is True
    assert _evaluate(f"ui.roomBindingsEditBlockReason({settled})") == ""

    for status in ("paused", "completed", "error"):
        assert _evaluate(f"ui.canEditRoomBindings({json.dumps({'status': status})})") is True

    for status in ("creating", "initializing", "discussing"):
        room = json.dumps({"status": status})
        assert _evaluate(f"ui.canEditRoomBindings({room})") is False
        assert _evaluate(f"ui.roomBindingsEditBlockReason({room})") == "room_not_settled"

    calling = json.dumps(
        {"status": "ready", "metadata": {"model_call": {"status": "calling"}}}
    )
    assert _evaluate(f"ui.canEditRoomBindings({calling})") is False
    assert _evaluate(f"ui.roomBindingsEditBlockReason({calling})") == "turn_in_flight"

    running_protocol = json.dumps(
        {"status": "ready", "protocol_state": {"status": "running"}}
    )
    assert _evaluate(f"ui.canEditRoomBindings({running_protocol})") is False
    assert _evaluate(f"ui.roomBindingsEditBlockReason({running_protocol})") == (
        "protocol_running"
    )
