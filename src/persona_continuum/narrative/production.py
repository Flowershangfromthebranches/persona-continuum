"""Production Package builder for AI video workflows.

Aggregates the Character/Location/Prop Visual Bibles from the story bible,
derives a timed shot list from the beat sheet, and emits model-agnostic
image/video generation prompts. No specific video vendor is targeted.
"""

from __future__ import annotations

from typing import Any

from persona_continuum.domain.narrative import (
    Beat,
    EpisodeVersion,
    NarrativeFormat,
    NarrativeScene,
    ProductionPackage,
    Shot,
    StoryBible,
)


class ProductionPackageBuilder:
    def build(
        self,
        project_id: str,
        episode_number: int,
        version: EpisodeVersion,
        bible: StoryBible,
        scenes: list[NarrativeScene],
        *,
        fmt: NarrativeFormat = NarrativeFormat.MICRO_DRAMA,
        visual_identity: dict[str, Any] | None = None,
    ) -> ProductionPackage:
        visual_identity = visual_identity or {}
        character_bible = self._character_visual_bible(bible)
        location_bible = self._location_visual_bible(bible)
        prop_bible = self._prop_visual_bible(bible, version)

        shots = self._shot_list(version.beat_sheet, scenes, character_bible, location_bible)

        dialogue_track = [
            {
                "shot": shot.shot_number,
                "start_time": shot.start_time,
                "character": speaker,
                "line": text,
            }
            for shot in shots
            for speaker, text in [self._first_dialogue(shot.dialogue)]
            if text
        ]
        subtitle_track = [
            {
                "start_time": d["start_time"],
                "end_time": shot.end_time,
                "text": d["text"],
            }
            for shot in shots
            for d in [self._subtitle_entry(shot, dialogue_track)]
            if d
        ]

        image_prompts = [
            f"{shot.visual_prompt}" for shot in shots if shot.visual_prompt
        ]
        video_prompts = [
            f"{shot.visual_prompt} Motion: {shot.motion_prompt or shot.movement or 'static camera'}"
            for shot in shots
            if shot.visual_prompt
        ]
        # Model-agnostic generic motion/visual guidance mirrors the legacy
        # video_generation_prompts list so downstream profile-specific
        # compilers can read intent without parsing vendor-flavored strings.
        generic_video_guidance = list(video_prompts)

        return ProductionPackage(
            project_id=project_id,
            episode_number=episode_number,
            episode_version_id=version.id,
            format=fmt,
            screenplay=version.screenplay,
            shot_list=shots,
            scene_list=[
                {
                    "order": s.order,
                    "location": s.location,
                    "time": s.time,
                    "participants": [p.name or p.character_id for p in s.participants],
                    "goal": s.scene_goal,
                }
                for s in scenes
            ],
            character_list=[
                {
                    "id": c.id,
                    "name": c.name,
                    "role": c.role,
                    "visual": c.visual,
                }
                for c in bible.characters
            ],
            location_list=[
                {
                    "id": loc.id,
                    "name": loc.name,
                    "description": loc.description,
                    "visual": loc.visual,
                }
                for loc in bible.locations
            ],
            prop_list=prop_bible["props"],
            dialogue_track=dialogue_track,
            subtitle_track=subtitle_track,
            sound_effect_plan=self._sfx_plan(shots),
            bgm_direction=self._bgm_direction(bible),
            image_generation_prompts=image_prompts,
            video_generation_prompts=video_prompts,
            generic_video_guidance=generic_video_guidance,
            character_visual_bible=character_bible["characters"],
            location_visual_bible=location_bible["locations"],
            prop_visual_bible=prop_bible["entries"],
            continuity_notes=self._continuity_notes(version, shots),
        )

    # ------------------------------------------------------------------
    def _character_visual_bible(self, bible: StoryBible) -> dict[str, Any]:
        characters = []
        for c in bible.characters:
            visual = dict(c.visual or {})
            visual.setdefault("forbidden_variations", [])
            visual.setdefault("color_palette", [])
            characters.append({"id": c.id, "name": c.name, **visual})
        return {"characters": characters}

    def _location_visual_bible(self, bible: StoryBible) -> dict[str, Any]:
        locations = []
        for loc in bible.locations:
            visual = dict(loc.visual or {})
            visual.setdefault("forbidden_variations", [])
            entry = {"id": loc.id, "name": loc.name, **visual}
            if loc.description and not entry.get("description"):
                entry["description"] = loc.description
            locations.append(entry)
        return {"locations": locations}

    def _prop_visual_bible(self, bible: StoryBible, version: EpisodeVersion) -> dict[str, Any]:
        props: list[str] = []
        text = version.screenplay.lower()
        for loc in bible.locations:
            for prop in loc.visual.get("props", []):
                if str(prop).lower() in text and str(prop) not in props:
                    props.append(str(prop))
        entries: list[dict[str, Any]] = [
            {"name": prop, "visual": next(
                (loc.visual.get("prop_visuals", {}).get(prop, {}) for loc in bible.locations),
                {},
            )}
            for prop in props
        ]
        return {"props": props, "entries": entries}

    def _shot_list(
        self,
        beat_sheet: list[Beat],
        scenes: list[NarrativeScene],
        character_bible: dict[str, Any],
        location_bible: dict[str, Any],
    ) -> list[Shot]:
        shots: list[Shot] = []
        shot_number = 1
        chars_by_name = {c["name"]: c for c in character_bible["characters"]}
        locs_by_name = {loc["name"]: loc for loc in location_bible["locations"]}

        if not scenes:
            # No simulated scenes: derive one shot per beat directly.
            for beat in beat_sheet:
                present = [c["name"] for c in character_bible["characters"]][:3]
                shots.append(
                    Shot(
                        shot_number=shot_number,
                        start_time=self._fmt_time(beat.start_seconds or 0),
                        end_time=self._fmt_time(beat.end_seconds or 0),
                        duration_seconds=float(
                            max((beat.end_seconds or 0) - (beat.start_seconds or 0), 1)
                        ),
                        shot_size="medium",
                        action=beat.description or beat.title,
                        visual_prompt=self._compose_visual_prompt(
                            "", {}, present, chars_by_name, beat
                        ),
                        motion_prompt=self._compose_motion_prompt(beat),
                        negative_constraints=["no text overlays", "no watermark"],
                        sfx=self._beat_sfx(beat),
                    )
                )
                shot_number += 1
            return shots

        for scene in sorted(scenes, key=lambda s: s.order):
            loc_visual = locs_by_name.get(scene.location, {})
            scene_beats = [
                b
                for b in beat_sheet
                if scene.scene_goal and b.description and scene.scene_goal in b.description
            ] or beat_sheet[:1]
            for beat in scene_beats:
                present = [p.name or p.character_id for p in scene.participants]
                visual_prompt = self._compose_visual_prompt(
                    scene.location, loc_visual, present, chars_by_name, beat
                )
                dialogue_text = "; ".join(
                    f"{line.get('speaker')}: {line.get('text')}" for line in scene.dialogue
                )
                shots.append(
                    Shot(
                        shot_number=shot_number,
                        start_time=self._fmt_time(beat.start_seconds or 0),
                        end_time=self._fmt_time(beat.end_seconds or 0),
                        duration_seconds=float(
                            max((beat.end_seconds or 0) - (beat.start_seconds or 0), 1)
                        ),
                        shot_size="medium" if len(present) <= 2 else "wide",
                        camera="main",
                        movement="handheld" if beat.title in ("Escalation", "Turn") else "static",
                        characters=present,
                        action=beat.description or beat.title,
                        dialogue=dialogue_text,
                        location=scene.location,
                        visual_prompt=visual_prompt,
                        motion_prompt=self._compose_motion_prompt(beat),
                        negative_constraints=[
                            "no text overlays",
                            "no watermark",
                            *(
                                loc_visual.get("forbidden_variations", [])
                                if isinstance(loc_visual, dict)
                                else []
                            ),
                        ],
                        continuity_constraints=[
                            f"characters present: {', '.join(present)}" if present else "",
                            f"location lock: {scene.location}",
                        ],
                        sfx=self._beat_sfx(beat),
                        transition="cut",
                    )
                )
                shot_number += 1
        return shots

    def _compose_visual_prompt(
        self,
        location: str,
        loc_visual: dict[str, Any],
        present: list[str],
        chars_by_name: dict[str, Any],
        beat: Beat,
    ) -> str:
        parts = [location or "interior space"]
        if loc_visual.get("style"):
            parts.append(str(loc_visual["style"]))
        for name in present:
            char = chars_by_name.get(name)
            if char:
                desc = ", ".join(
                    str(char.get(k))
                    for k in ("age", "gender", "face", "hair", "costume")
                    if char.get(k)
                )
                if desc:
                    parts.append(f"{name} ({desc})")
        parts.append(beat.title.lower())
        return "; ".join(p for p in parts if p)

    def _compose_motion_prompt(self, beat: Beat) -> str:
        tension = {
            "Hook": "quick push-in",
            "Escalation": "handheld follow",
            "Reversal": "whip pan to reaction",
            "Cliffhanger": "slow push-in on face",
        }.get(beat.title, "subtle drift")
        return tension

    def _beat_sfx(self, beat: Beat) -> list[str]:
        mapping = {
            "Hook": ["riser"],
            "Reversal": ["impact", "sting"],
            "Cliffhanger": ["low drone", "heartbeat"],
        }
        return mapping.get(beat.title, ["ambient"])

    def _bgm_direction(self, bible: StoryBible) -> str:
        tone = ", ".join(bible.tone) if bible.tone else "cinematic"
        return f"Tone: {tone}. Underscore follows beat tension; swell at Reversal and Cliffhanger."

    def _continuity_notes(self, version: EpisodeVersion, shots: list[Shot]) -> list[str]:
        notes = [
            f"Canon episode EP{version.episode_number} v{version.version}; "
            "keep costume, hair, and palette locked to visual bibles.",
        ]
        locations_seen: list[str] = []
        for shot in shots:
            if shot.location and shot.location not in locations_seen:
                locations_seen.append(shot.location)
        notes.append("Location order: " + " -> ".join(locations_seen))
        return notes

    @staticmethod
    def _fmt_time(seconds: int) -> str:
        return f"{seconds // 60:02d}:{seconds % 60:02d}"

    @staticmethod
    def _first_dialogue(dialogue: str) -> tuple[str, str]:
        if not dialogue:
            return "", ""
        first = dialogue.split(";")[0]
        if ":" in first:
            speaker, text = first.split(":", 1)
            return speaker.strip(), text.strip()
        return "", first.strip()

    def _subtitle_entry(
        self, shot: Shot, dialogue_track: list[dict[str, Any]]
    ) -> dict[str, Any] | None:
        for entry in dialogue_track:
            if entry["shot"] == shot.shot_number:
                return {
                    "start_time": entry["start_time"],
                    "end_time": shot.end_time,
                    "text": entry["line"],
                }
        return None

    def _sfx_plan(self, shots: list[Shot]) -> list[dict[str, Any]]:
        return [
            {"shot": shot.shot_number, "start_time": shot.start_time, "sfx": shot.sfx}
            for shot in shots
            if shot.sfx
        ]
