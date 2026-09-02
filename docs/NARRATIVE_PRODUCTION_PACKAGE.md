# Narrative Production Package

`narrative/production.py` turns a canon episode version into a structured
package that AI video tools can consume. It is **model-agnostic** — prompts are
plain text, with no vendor binding.

## Contents

- screenplay + timed beat sheet
- `shot_list`: per shot — number, start/end/duration, shot size, camera,
  movement, characters, action, dialogue, visual_prompt, motion_prompt,
  negative/continuity constraints, sfx, bgm, transition
- scene/character/location/prop lists
- dialogue track + subtitle track (timed)
- sound-effect plan, BGM direction (derived from bible tone)
- image & video generation prompts per shot
- **Visual Bibles**: Character / Location / Prop, aggregated from the story
  bible `visual` dictionaries (age/gender/face/hair/costume/color palette/
  forbidden variations) for cross-episode visual consistency
- continuity notes (canon version, costume/hair/palette lock, location order)

## Canon requirement

A **final** production package may only be derived from a committed canon episode
version. `generate_production_package` raises
`NARRATIVE_PRODUCTION_CANON_REQUIRED` when the episode has no canon master —
it never silently falls back to the latest draft. Pass `is_preview=True` to
opt into the legacy degradation explicitly: the package is then marked
`is_preview=True` (UI badge **PREVIEW / NON-CANON**) and is rejected as a
source for the shooting / model-prompt pipeline
(`SHOOTING_SOURCE_PREVIEW_NOT_ALLOWED`).

## Legacy prompts are intent, not deliverable

`video_generation_prompts` (and per-shot `visual_prompt` / `motion_prompt`) are
**model-agnostic motion/visual intent** — the Planner is never bound to a
vendor. They are NOT final prompts ready to submit to a specific video model.
Downstream, the model-specific generation plan lives in
`ModelPromptPackage` clips compiled per `VideoModelProfile` (see
`docs/NARRATIVE_VIDEO_PRODUCTION_PIPELINE.md`); the UI moves the legacy block
into an advanced, labelled fold-out.

## Example

```python
package = continuum.narratives.generate_production_package(project_id, episode_number=1)
package.shot_list[0].visual_prompt
package.video_generation_prompts  # one per shot (model-agnostic intent)
```

Shots are derived from the beat sheet; when scenes were simulated, participants,
location locks, and dialogue are folded into each shot; otherwise one shot per
beat is emitted. Tests: `tests/unit/test_narrative_production.py` and the E2E.
