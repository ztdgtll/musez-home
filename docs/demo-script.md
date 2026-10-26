# Demo script — 3 minutes (locked structure, Oct 26)

The submission video. Design decisions (Oct 26, with Javier):

- **Live room audio** for the interaction: Javier speaks 5 short, scripted
  English lines. No improvisation in a second language.
- **Voice-over** (synthetic narrator voice, English) carries the explanation
  between turns. Narration is our choice, not a rule requirement.
- **English subtitles across the whole video**, including any room reply
  that comes out in Spanish.
- Only what was built during the event window is shown as the product
  (the bridge: Spark routing, Wonder escalation, measured latency). The
  room itself — microphone, wall, speakers — is pre-existing infrastructure
  and is framed that way, here and in the README.
- Latency figures shown on screen come from the real takes (filled in at
  edit time from the bridge's own marks). Measured, not promised.

## The takes

| Time | Shot | Audio |
|---|---|---|
| 0:00–0:22 | Wide: the studio, Javier walks in, sits | VO 1 |
| 0:22–0:50 | Javier + wall in frame | **Line 1** → weather answer out loud |
| 0:50–1:12 | Close on the wall | **Line 2** → weather scene appears |
| 1:12–1:38 | Wide | **Line 3** → focus scene + short reply |
| 1:38–1:50 | Wall close, latency chip visible | VO 2 |
| 1:50–2:12 | Javier + wall | **Line 4** → escalation note on screen → Wonder's plan |
| 2:12–2:32 | Wall: the plan scene; latency table card | VO 3 |
| 2:32–2:45 | Wide | **Line 5** → room's goodbye |
| 2:45–3:00 | End card | VO 4 |

## Javier's lines (fixed — rehearsed Oct 31–Nov 1)

1. "Hey Muse, what's the weather like this weekend?"
   *(el clima del fin de semana — turno rápido con herramienta)*
2. "Put it on the wall."
   *(que lo ponga en la pared — escena con los datos)*
3. "Set the studio to focus mode."
   *(modo enfoque — otra escena, respuesta corta)*
4. "Plan my Saturday: a motorcycle ride in the morning, and home before the rain."
   *(el turno difícil — el puente escala a Wonder)*
5. "Thanks, Muse. That's all."
   *(cierre)*

## Voice-over script (English, ~115 words total)

**VO 1** (0:00) — "This is a room you can talk to. A microphone listens, a
Raspberry Pi runs the room, and Meta's Muse models do the thinking. I ask
out loud. The room answers out loud — and shows it on the wall."

**VO 2** (1:38) — "Simple questions take the fast path: Muse Spark,
streaming, sentence by sentence. But some requests need real planning.
Watch what the bridge does with this one."

**VO 3** (2:12) — "The bridge escalated that turn to Muse Wonder. Same
conversation, deeper model. And every stage of every turn is timed: first
token, first sentence, first sound."

**VO 4** (2:45) — "Built in ten days for the Meta Global AI Developer
Hackathon. MuseZ Home: measured, not promised."

## Production notes

- Two angles: room wide (Javier + wall together) and a wall close-up.
  Record the room's speaker output on its own track; Javier on a lav or
  the phone close by.
- Keep the room's acknowledgement choreography visible — it is part of
  the honesty story: the system shows it is thinking while it thinks.
- Edit trims silence, never numbers: the latency chips and the closing
  table carry the measured marks from the takes themselves.
- End card: project name, "Built Oct 26 – Nov 4, 2026", repo URL.

## Rehearsal checklist (Oct 31 – Nov 1)

- [ ] Run all 5 turns live in the room; drop or fix any turn that stalls.
- [ ] Record the "before" latency of the pre-hackathon chain (for the
      writeup table) if not already captured.
- [ ] Practice Javier's 5 lines to comfort, not perfection.
- [ ] Record VO with the chosen narrator voice; check names and numbers
      pronunciation ("Muse Spark", "Muse Wonder", figures).
- [ ] Two full takes minimum; keep the better one per segment.
