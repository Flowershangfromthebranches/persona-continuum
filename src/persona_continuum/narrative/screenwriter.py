"""Screenwriter Stage — converts simulation results into a commercial script.

The simulation output is NOT the final script. This stage reorganizes it into
a beat sheet whose timing adapts to the project format, episode duration,
genre, and author settings. No fixed template is hardcoded: the beat structure
is derived from the format and duration each call.
"""

from __future__ import annotations

from typing import Any

from persona_continuum.domain.narrative import (
    Beat,
    EpisodePlan,
    NarrativeFormat,
    NarrativeScene,
)


class ScreenwriterStage:
    DEFAULT_BEAT_TITLE = ("Hook", "Setup", "Escalation", "Reversal", "Payoff", "Cliffhanger")

    def beat_sheet_for(
        self,
        plan: EpisodePlan,
        duration_seconds: int,
        fmt: NarrativeFormat = NarrativeFormat.SERIES,
    ) -> list[Beat]:
        """Derive a timed beat sheet from the plan, duration, and format."""
        if plan.beats:
            beats = [b.model_copy(deep=True) for b in plan.beats]
        else:
            titles = list(self.DEFAULT_BEAT_TITLE)
            if fmt == NarrativeFormat.NOVEL:
                titles = ["Opening Image", "Rising Tension", "Turn", "Crisis", "Resolution"]
            beats = [Beat(order=i + 1, title=t) for i, t in enumerate(titles)]

        n = max(len(beats), 1)
        cursor: float = 0
        slice_len = duration_seconds / n
        for i, beat in enumerate(beats):
            beat.order = i + 1
            beat.start_seconds = int(cursor)
            cursor += slice_len
            beat.end_seconds = int(cursor)
        # Cliffhanger/hook emphasis for micro drama: first and last beats short.
        if fmt == NarrativeFormat.MICRO_DRAMA and len(beats) >= 2:
            beats[0].end_seconds = min(
                beats[0].end_seconds or 0, MICRO_DRAMA_HOOK_SECONDS
            )
            beats[-1].start_seconds = max(
                beats[-1].start_seconds or 0,
                duration_seconds - MICRO_DRAMA_HOOK_SECONDS,
            )
            beats[-1].end_seconds = duration_seconds
        return beats

    def build_screenplay(
        self,
        plan: EpisodePlan,
        scenes: list[NarrativeScene],
        beat_sheet: list[Beat],
        *,
        title: str = "",
        format_label: str = "",
    ) -> str:
        """Render the simulation results as a shooting screenplay."""
        lines: list[str] = [f"# EP{plan.episode_number:02d}  {title or plan.title}", ""]
        if format_label:
            lines.append(f"*Format: {format_label}*")
        if plan.hook:
            lines.append(f"[HOOK] {plan.hook}")
        lines.append("")

        for beat in beat_sheet:
            window = f"{beat.start_seconds or 0}s-{beat.end_seconds or 0}s"
            lines.append(f"## Beat {beat.order}: {beat.title}  ({window})")
            if beat.description:
                lines.append(beat.description)
            lines.append("")

        if scenes:
            lines.append("## Scenes")
            for scene in sorted(scenes, key=lambda s: s.order):
                lines.append(
                    f"### Scene {scene.order} — {scene.location or 'UNSPECIFIED'}"
                    + (f" ({scene.time})" if scene.time else "")
                )
                if scene.scene_goal:
                    lines.append(f"*Goal:* {scene.scene_goal}")
                for line in scene.dialogue:
                    speaker = line.get("speaker", "?")
                    text = line.get("text", "")
                    lines.append(f"**{speaker}:** {text}")
                for decision in scene.decisions:
                    lines.append(
                        "> DECISION [{}]: {}".format(
                            decision.get("actor", "?"), decision.get("description", "")
                        )
                    )
                if scene.summary:
                    lines.append(f"*Exit:* {scene.summary}")
                lines.append("")
        if plan.cliffhanger:
            lines.append(f"[CLIFFHANGER] {plan.cliffhanger}")
        return "\n".join(lines).strip() + "\n"

    def draft_from_simulation(
        self,
        plan: EpisodePlan,
        scenes: list[NarrativeScene],
        simulation_summary: str,
        *,
        fmt: NarrativeFormat = NarrativeFormat.SERIES,
        title: str = "",
        duration_seconds: int | None = None,
    ) -> dict[str, Any]:
        duration = duration_seconds or plan.estimated_duration_seconds
        beat_sheet = self.beat_sheet_for(plan, duration, fmt)
        format_label = fmt.value.replace("_", " ")
        screenplay = self.build_screenplay(
            plan, scenes, beat_sheet, title=title, format_label=format_label
        )
        return {
            "beat_sheet": [b.model_dump(mode="json") for b in beat_sheet],
            "screenplay": screenplay,
            "simulation_summary": simulation_summary,
            "duration_seconds": duration,
        }


MICRO_DRAMA_HOOK_SECONDS = 15
