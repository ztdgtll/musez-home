# Live voice call from the studio Pi — 2026-11-05

The day Muse Voice launched for everyone in the US, the studio's
Raspberry Pi 5 (Muse Gadget SDK for Linux, node `homelink-08c4ab`)
was updated from its Oct 14 build to the current SDK (upstream
commit `270e653`, Nov 3), which adds a realtime voice-call client
(`musegadget voice-call`). A full backup — pairing data included —
was taken first; after the update the service was healthy and the
pairing intact.

The test ran in WAV mode (no room audio): a synthesized question
in, the spoken reply out.

- **Question** (`voice-call-2026-11-05-question.wav`): «Hola, esto
  es una prueba desde el estudio. ¿Cuánto es dos más dos?» —
  4.75 s, PCM16LE mono 24 kHz, the SDK's documented input format.
- **Session**: the Pi established its voice session on
  2026-11-05 15:09:06 ET and the call reached the account's Muse
  directly from the Pi — no phone involved.
- **Reply** (`voice-call-2026-11-05-reply.wav`): Muse answered
  out loud — «Son cuatro, Javier. Te escucho perfecto desde el
  estudio, prueba superada.» — 5.72 s of speech (RMS 2431,
  peak 20374; silence would sit near zero).
- **End**: the client closed the call itself with `idle_timeout`,
  exit code 0.

What this proves: the gadget-to-Muse realtime voice path, end to
end, on the room's own hardware. What it does not prove yet:
`--live` microphone/speaker routing in the room — the current
turn-based system holds the microphone, and handing it to the call
is milestone 1 of the integration described in the README's
Nov 5 update.
