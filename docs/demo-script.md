# Demo script — 3 minutes (revised Oct 29)

The submission video. Design decisions (Oct 26, revised Oct 29 with Javier):

- **Three spoken lines**, each self-contained and starting with the wake
  word — the room has no conversation mode; every turn begins with
  "Hey Muse". Nothing in the edit may imply otherwise.
- **Live room audio** for the interaction: Javier speaks the three lines
  in English; the room answers in Spanish, by design (see VO 2).
- **Voice-over** (synthetic narrator voice, English) carries the story
  between turns. **English subtitles across the whole video**; in wall
  close-ups, subtitles show the wall's actual lines, not just the audio.
- Cut from the earlier draft: the focus-mode line (no such capability)
  and the old-vs-new latency contrast (decided Oct 29 — not filmed).
- Latency figures shown on screen come from the real takes' own logs —
  measured, not promised. Edit trims silence, never numbers.
- One **declared warm-up turn** (a disposable Spark call) runs before
  each take and is listed in the production checklist below — never
  hidden. Cold and warm turns are published separately in the results.

## The rundown

| Time | Shot | Audio / content |
|---|---|---|
| 0:00–0:05 | Wall close: **the Saturday plan infographic is already up** — the day drawn as a timeline, the rain band marked | VO 1 hook |
| 0:05–0:35 | Wide: the studio as it is; Javier in the room | VO 1 — the room story (provenance, once) |
| 0:35–1:05 | Javier + wall; simple on-screen diagram of the bridge | VO 2 — the bridge, simply + the bilingual-design line |
| 1:05–1:35 | Javier + wall in frame | **Line 1** → weekend weather answer out loud; the weather lands on the wall as one final scene, with the voice |
| 1:35–2:20 | Javier + wall, then wall close | **Line 2** → the turn escalates; Wonder's Saturday plan renders on the wall as an **infographic** (timeline of the day, rain band, exact times). Wonder evidence overlay on screen (post-production, see below) |
| 2:20–2:45 | Wall + Javier | VO 3 — what was measured; the repo; the switch |
| 2:45–2:52 | Wide | **Line 3** → the room's goodbye |
| 2:52–3:00 | End card | "MuseZ Home: measured, not promised" |

**Vertical cut (Most Viral):** a 30 s 9:16 version that starts with the
same infographic reveal (0:00–0:05 shot), takes the plan turn, and
closes on the end card. Planned from the same takes, not an afterthought.

## Javier's lines (fixed — rehearsed Oct 31–Nov 1)

1. "Hey Muse, what's the weather like this weekend? Put it on the wall."
   *(clima del finde, directo a la pared — turno rápido con herramienta)*
2. "Hey Muse, plan my Saturday: «actividad real de la mañana — a elección
   de Javier», and home before the rain."
   *(el turno difícil — el puente escala a Wonder; la actividad la elige
   Javier de su sábado real antes del ensayo y la línea queda fija)*
3. "Hey Muse, that's all. Thanks."
   *(cierre)*

## The plan scene is an infographic

Wonder generates the plan's **content and structure** from the real
7-day forecast (times, the dry window, the rain band). The wall
**draws** it with its own exact graphics and type — timeline, rain
band, temperatures — so every letter and number on screen is exact.
An AI-generated image never carries the data (image models garble
text); the surprise is a wall that lays out your whole Saturday, not
a text card.

## Wonder evidence overlay (post-production)

During the plan turn, an overlay generated from the turn's own log
shows: **turn ID · exact model ID (`muse-wonder-1.0`) · escalation
reason · tool calls · elapsed time**. It is an editing overlay and
sits **outside the projected disc** — the wall itself only ever shows
the room's own scenes. Same rule for any latency chip or table:
post-production graphics from the logs, outside the disc.

## Voice-over script (English, ~120 words total)

**VO 1** (0:00) — "This is a room you can talk to — one I've been
building for years: a wall that listens, a voice that answers. This
week, it got a new brain."

**VO 2** (0:35) — "MuseZ Home is a bridge to Meta's Muse models. Muse
Spark takes the everyday turns, streaming, in seconds. When a request
needs real planning, the bridge hands it to Muse Wonder. She answers
in Spanish by design: scene titles in English, her lines in Spanish."

**VO 3** (2:20) — "Every stage of every turn is timed and logged —
first token, first sentence, first sound. The code, the measurements
and this room's bridge are public. When the event ends, one switch
turns the paid API off — and the room works exactly as it did before."

## Production checklist

**The switch (la llave)**
- [ ] API **ON** for every rehearsal turn and take: the bridge thinks
      (Spark/Wonder) and spends credit.
- [ ] API **OFF** = no paid calls at all: the room works exactly as it
      did before the bridge (classic path), free.
- [ ] After the hackathon submission (Nov 4): flip the API **off**.
      Zero spend from then on; flip on only when the fast brain is wanted.

**Takes**
- [ ] One declared warm-up turn before each take (listed, not hidden).
- [ ] Run all 3 turns live in the room; drop or fix any turn that stalls.
- [ ] Practice the 3 lines to comfort, not perfection; line 2's activity
      is fixed with Javier before rehearsing.
- [ ] Two angles: room wide (Javier + wall) and wall close-up. Record
      the room's speaker output on its own track; Javier on a lav or
      the phone close by.
- [ ] Keep the room's acknowledgement choreography visible — the system
      shows it is thinking while it thinks.
- [ ] Record the VO; check name and number pronunciation ("Muse Spark",
      "Muse Wonder", figures).
- [ ] Two full takes minimum; keep the better one per segment.
- [ ] End card: "MuseZ Home: measured, not promised", "Built Oct 26 –
      Nov 4, 2026", repo URL.
- [ ] Cut the 30 s vertical version from the same takes.
