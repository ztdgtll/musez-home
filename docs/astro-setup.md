# Astro dispatch setup (`call_astro`)

The bridge tool `call_astro` sends **Astro** (Amazon's household
robot) to a destination in the home — the studio or the kitchen —
so it can bring or take away a small item in its cargo compartment
(a bottle of water in, an empty bottle out). Astro has no public
API and does not vacuum; the dispatch chain the bridge uses is:

```
bridge ──HTTPS GET──▶ Voice Monkey Routine Trigger (virtual device)
        ──▶ Alexa routine ──Custom action──▶ "Go to <room/viewpoint>"
        ──▶ Astro navigates to the viewpoint
```

Firing the trigger URL is all the bridge does. Success means Voice
Monkey answered HTTP 200 — the routine ran. (Whether Astro then
completes the trip depends on the house: closed doors, obstacles,
a map that doesn't cover the destination.)

> **The trigger URLs are secrets.** Each one embeds a Voice Monkey
> API token. They live only in environment variables on the machine
> that runs the bridge — never in this repo, never in a screenshot,
> never in chat. Every URL below is a placeholder.

## 1. Voice Monkey: account + Routine Trigger devices

1. Create an account at <https://voicemonkey.io> and link the
   **Voice Monkey** Alexa skill to the same Amazon account Astro
   uses (Alexa app → Skills → Voice Monkey → Enable → sign in).
2. In the Voice Monkey console, create one **Routine Trigger**
   device per destination, e.g.:
   - `astro-studio`
   - `astro-kitchen`
3. For each device, open the **Playground** / trigger URL and copy
   its full trigger URL. It looks like:

   ```
   https://api-v3.voicemonkey.io/trigger?token=<YOUR-TOKEN>&device=<DEVICE-ID>
   ```

   Keep these somewhere private (a password manager) — step 4 is
   the only place they are entered.

## 2. Astro app: create the viewpoints

Astro navigates to **viewpoints** — saved poses (position + facing)
on its single-floor map. Create one per destination:

1. Astro app → **Live View** → **Start Live View**.
2. Drive Astro to the exact spot for the destination (e.g. inside
   the studio door, facing the desk; in the kitchen, facing the
   counter) and aim its camera where you want it to look on
   arrival.
3. **Viewpoints** → **Create a new viewpoint** → name it, e.g.
   `studio` and `kitchen`.

Notes:

- Astro maps **one floor** — the destinations must be on the
  floor Astro lives on; it cannot use stairs.
- The trip works best with the doors on the path open and the
  floor clear.
- Someone has to load and unload the cargo compartment: Astro
  carries, it does not pick things up.

## 3. Alexa app: one routine per destination

For each destination, in the Alexa app:

1. **More → Routines → +** (new routine), name it e.g.
   `MuseZ — Astro to the studio`.
2. **When this happens → Smart Home** → **Alexa Voice Monkey v3**
   → **VM Routines** → pick the event for the trigger device from
   step 1 (`astro-studio`). (Voice Monkey triggers are event
   sources, not ordinary smart-home devices — they only appear
   under that VM Routines list, not in the device list.)
3. **Add action → Custom** and type the command **without**
   Astro's name:

   ```
   Go to Study Room
   ```

   (the destination exactly as Astro's map names it — room or
   viewpoint). Set **Hear Alexa from** to **Astro** itself.

   > **Verified live, 2026-11-02 — read this before "fixing" the
   > text.** The intuitive version, `Astro, go to Study Room`,
   > silently does nothing: the routine fires (it shows in
   > Routines → Activity) but Astro never moves — with the output
   > device set to Astro *or* to an Echo. Dropping the `Astro,`
   > prefix, with the output device on Astro, is the combination
   > that works: Astro left the living room and navigated to the
   > study on the first try. Custom actions are delivered to the
   > device as the command *after* its wake word, so the name
   > must not be in the text.
4. Save. Repeat with the kitchen trigger and `Go to Kitchen`
   the same way.

Test each routine once by pressing the Voice Monkey device's test
trigger (or by opening its trigger URL in a browser where you are
signed in): Astro should leave its charger and drive to the
viewpoint. Only wire the bridge (step 4) once both trips work —
the bridge cannot fix a routine that doesn't move the robot.

## 4. Bridge configuration

On the machine that runs the bridge daemon, set the two trigger
URLs in the daemon's environment (systemd unit `Environment=` /
environment file, shell profile — wherever that deployment keeps
its config; the Roborock segment variable `MUSEZ_STUDIO_SEGMENT`
lives in the same place):

```bash
MUSEZ_ASTRO_URL_STUDIO="https://api-v3.voicemonkey.io/trigger?token=<YOUR-TOKEN>&device=<STUDIO-DEVICE-ID>"
MUSEZ_ASTRO_URL_KITCHEN="https://api-v3.voicemonkey.io/trigger?token=<YOUR-TOKEN>&device=<KITCHEN-DEVICE-ID>"
```

Then restart the bridge daemon so it picks up the environment.

- A destination whose variable is unset is simply unavailable:
  `call_astro` returns an honest `{"ok": false, ...}` result
  naming the missing variable, and the turn answers accordingly —
  nothing is faked.
- Adding a destination later = one more Voice Monkey trigger, one
  more viewpoint, one more routine, one entry in
  `ASTRO_URL_ENVS` (bridge/studio_room.py) and one more env var.

## 5. Try it

Say (or type, in a dry run): *"Hey Muse, send Astro to the studio
with my water bottle"* — the model calls
`call_astro {"destination": "studio"}`, the bridge fires the
studio trigger, and Astro rolls in. For the return trip:
*"Astro can take the empty bottle back to the kitchen"* →
`call_astro {"destination": "kitchen"}`.
