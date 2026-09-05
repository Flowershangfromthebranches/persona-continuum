// Pure Room UI lifecycle rules shared by the browser controller and Node tests.
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.PersonaRoomUiState = api;
})(typeof window !== "undefined" ? window : null, function () {
  // Mirrors backend TERMINAL_RUN_STATUSES: waiting_clarification ends the
  // run on purpose (the host asked the user one indispensable question).
  const TERMINAL_PROTOCOL_STATUSES = new Set([
    "success",
    "partial_success",
    "failed",
    "cancelled",
    "waiting_clarification",
  ]);

  function isProtocolTerminal(status) {
    return TERMINAL_PROTOCOL_STATUSES.has(String(status || ""));
  }

  function protocolStageForDisplay(runtime) {
    const state = runtime || {};
    const status = String(state.status || "");
    // A successful run keeps its final answer on screen instead of the
    // idle "waiting_user" stage.
    if (["success", "partial_success"].includes(status)) return "final_response";
    if (status === "waiting_clarification") return "waiting_user";
    if (status === "cancelled") return "waiting_user";
    if (status === "failed") return "failed";
    return String(state.current_stage || "waiting_user");
  }

  function shouldSettleBusy(room) {
    if (!room) return false;
    if (room.status === "error" || room.last_error) return true;
    const call = (room.metadata && room.metadata.model_call) || {};
    if (["completed", "error", "cancelled", "interrupted"].includes(call.status)) return true;
    const runtime = room.protocol_state || {};
    return isProtocolTerminal(runtime.status) && room.status !== "discussing";
  }

  function shouldStopWatch(room) {
    if (!room) return true;
    if (["completed", "error", "paused"].includes(room.status)) return true;
    const runtime = room.protocol_state || {};
    if (isProtocolTerminal(runtime.status) && room.status === "ready") return true;
    return !["discussing", "initializing"].includes(room.status)
      && runtime.status !== "running";
  }

  function shouldStartLegacyAutonomous(room, hasModelOutput, trigger) {
    if (!room) return false;
    return room.protocol === "free_discussion"
      && ["autonomous", "auto"].includes(room.mode)
      && room.status === "ready"
      && !hasModelOutput
      && trigger === "user_message";
  }

  function canCancelTurn(room) {
    if (!room) return false;
    const call = (room.metadata && room.metadata.model_call) || {};
    const runtime = room.protocol_state || {};
    return room.status === "discussing"
      || call.status === "calling"
      || runtime.status === "running";
  }

  function canResumeRoom(room) {
    return !!room && ["paused", "completed"].includes(String(room.status || ""));
  }

  // Binding edit gate: provider/model/effort may only change while no turn
  // generation or protocol run is in flight.  Returns "" when editable,
  // otherwise a stable reason code used by the UI disable hint and tests.
  function roomBindingsEditBlockReason(room) {
    if (!room) return "no_room";
    const status = String(room.status || "");
    if (!["ready", "paused", "completed", "error"].includes(status)) {
      return "room_not_settled";
    }
    const call = (room.metadata && room.metadata.model_call) || {};
    if (["calling", "streaming"].includes(String(call.status || ""))) {
      return "turn_in_flight";
    }
    const runtime = room.protocol_state || {};
    if (String(runtime.status || "") === "running") return "protocol_running";
    return "";
  }

  function canEditRoomBindings(room) {
    return roomBindingsEditBlockReason(room) === "";
  }

  function shouldShowProtocolPanel(room) {
    return !!room && String(room.protocol || "free_discussion") !== "free_discussion";
  }

  function enabledParticipants(room) {
    return ((room && room.participants) || []).filter(p => p && p.enabled !== false);
  }

  // Structural upgrade hint: a free_discussion room with at least two
  // enabled participants is a fit for expert_consultation.  A host is NOT
  // required — when none exists the upgrade flow asks the user to pick one
  // (see protocolUpgradeRoleMapping).  Purely structural — no hard-coded
  // room titles.
  function shouldSuggestProtocolUpgrade(room) {
    if (!room || room.protocol !== "free_discussion") return false;
    return enabledParticipants(room).length >= 2;
  }

  // Role plan for the free_discussion -> expert_consultation upgrade.
  // Pure data for the confirm dialog and the convert request body:
  //   hostId              participant that keeps/becomes the single host,
  //                       or null when the room has no host yet
  //   needsHostSelection  true -> the user must pick one of `candidates`
  //   candidates          enabled participant ids eligible to become host
  //   mapping             {participantId: "host"|"expert"} covering every
  //                       enabled participant; while needsHostSelection is
  //                       true every entry is a provisional "expert" and
  //                       the picked host overrides one entry afterwards.
  function protocolUpgradeRoleMapping(room) {
    const active = enabledParticipants(room);
    const mapping = {};
    active.forEach(p => { mapping[p.participant_id] = "expert"; });
    const hosts = active.filter(p => p.role === "host");
    if (hosts.length >= 1) {
      const hostId = hosts[0].participant_id;
      mapping[hostId] = "host";
      return { hostId, needsHostSelection: false, candidates: [], mapping };
    }
    return {
      hostId: null,
      needsHostSelection: true,
      candidates: active.map(p => p.participant_id),
      mapping,
    };
  }

  return {
    isProtocolTerminal,
    protocolStageForDisplay,
    shouldSettleBusy,
    shouldStopWatch,
    shouldStartLegacyAutonomous,
    canCancelTurn,
    canResumeRoom,
    canEditRoomBindings,
    roomBindingsEditBlockReason,
    shouldShowProtocolPanel,
    shouldSuggestProtocolUpgrade,
    protocolUpgradeRoleMapping,
  };
});
