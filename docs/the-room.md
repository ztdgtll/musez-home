# The room it runs in

*Context for the submission: this room pre-dates the hackathon. What the
event added is the bridge — see the README. This page exists because the
stage shapes everything the bridge does: its latency targets, its scene
grammar, even its typography are answers to this room's physics.*

## The arc

MuseZ Home is the fourth chapter of a longer build — one builder, one
idea, four bodies:

1. **The mirror (Raspberry Pi 4).** It started as a smart mirror: a
   MagicMirror display that showed the day at a glance. A wall that
   informs — but a wall you can only look at.
2. **The companions (ESP32).** The presence shrank: a small desktop
   device with the same face, an avatar that could sit next to you
   instead of hanging on a wall. Presence, in miniature.
3. **The studio (Raspberry Pi 5).** The current room: the mirror became
   a projected wall in a music studio, gained ears (a microphone array,
   a local wake word) and a voice (a speaker, a speech pipeline, a
   watchdog that verifies answers are actually spoken). A wall you can
   talk to.
4. **The brain (this event).** The bridge: the room's requests now go to
   a dedicated, measured pipeline on Meta's Muse models — Spark for the
   fast path, Wonder when a turn needs to think — instead of a general
   chat path. Same room, new brain.

## Two layers of light

The wall is not a screen. It is two lights composed on plaster:

- **Layer 1 — the disc.** A sunset lamp paints a large, warm orange
  disc on the wall. It is always there; it is the stage.
- **Layer 2 — the projection.** A small MEMS laser projector draws in
  white over pure black. Its black emits no light at all, so the orange
  disc shows *through* the black — and the white interface floats inside
  the circle. The composition happens in light, on the wall, not in
  software.

That physics dictates the interface: everything lives inside the disc,
nothing exists outside it; content is drawn at 100% intensity because
the projector is dim (~120 lumens) and the orange wash would swallow
anything dimmer; strokes and type are large and thick for the same
reason.

### Why this projector

The projector is a Mimono HD-501M, a MEMS laser-scanning unit. It was
chosen over brighter short-throw projectors for one property: **true
black**. A conventional projector lights up its whole rectangle,
including the "black" parts — the frame is always visible and the
illusion dies. The MEMS scanner draws only what is lit, so the
rectangle disappears and the interface floats in the disc. It runs at
an effective 720p, 1:1, which at the room's viewing distance is the
honest resolution — and all the design needs.

## The design language

The wall follows a closed set of criteria, in the functional-minimalist
tradition (Apple, Braun, Teenage Engineering are the builder's stated
references):

- **Her line.** The assistant's avatar is the spatial anchor of every
  composition. Anything that is *her voice* — a reply, a verdict, an
  announcement — aligns to her line, her vertical center, so speech
  reads as one group: her, and her words.
- **Two groups, never mixed.** Material (scene titles, photos, data)
  lives above, on the scene's axis. Her voice lives on her line. An
  element that belongs to no axis is an element in the wrong place.
- **Nothing overlaps, ever.** The avatar's corner is an exclusion zone;
  every composition is verified against the real projected disc.
- **A closed type scale.** A handful of sizes on an 0.8-unit spacing
  grid; hierarchy comes from size and weight only — never from grays or
  opacity, which the projector would wash out anyway.
- **Type.** A Helvetica-line grotesque for text (Nimbus Sans on the
  Pi); a mono face appears only where real data is shown (feeds,
  readings). Scene titles are in English; everything else, in Spanish.
- **Imagery** is rectangular, large, with softly diffused edges — no
  repeated circles competing with the one circle that matters.

## The voice

The room is used by talking, so the spoken reply is designed with
the same discipline as the wall:

- **One gesture.** Voice and wall land together, in a single
  dispatch under the turn's own id: the scene is not a subtitle
  for the voice, and the voice is not a narration of the scene —
  they are one answer arriving on two surfaces at once.
- **Never silence without a sign of life.** The moment a request
  ends, the room acknowledges it — a thinking state on the wall
  and a short spoken receipt — and if a turn runs long, progress
  lines keep arriving. A silent room reads as a broken room; this
  one learned that from real failures.
- **A reply is not done until it is heard.** A watchdog verifies,
  from evidence on the Pi, that every spoken turn actually
  played through the speaker. If one didn't, the system says so
  and recovers: an answer that never sounded is treated as an
  answer that never happened.
- **Sentences, not paragraphs.** Replies are composed to be
  spoken — closed sentences, in order, playback starting at
  sentence zero while the rest stream in behind. Text written
  for eyes reads badly out loud; this text is written for ears.
- **One voice, every body.** The room speaks with the same voice
  as the assistant on the builder's phone and computer, in the
  language of the request — Spanish in daily life, English when
  asked. The room is not a second assistant with a second
  character; it is the same one, standing somewhere new.

## The body, today

A Raspberry Pi 5 runs the room: the projected wall (hosted on
MagicMirror², the open-source modular display platform, MIT), the
state-driven scene page, the wake-word listener on a USB microphone
array, speech out through a room speaker, and the response watchdog.
Scheduled quiet hours keep the wall dark overnight; a finished result
stays on the wall until the builder replaces it — states respect work.

## Credits

The ambient display is hosted on
[MagicMirror²](https://github.com/MagicMirrorOrg/MagicMirror)
(open source, MIT). The composition criteria, scene system, voice
pipeline and state machinery around it are the builder's own — as is
everything in this repository.
