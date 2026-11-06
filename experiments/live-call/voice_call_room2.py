"""Instrumented realtime voice call through the studio's ALSA path.

Same as voice_call_room.py, plus Astra's point-C instrumentation:
- Every input_audio_buffer.append that actually goes out is decoded and
  written to a C file (the exact PCM sent to the server, after all gates),
  with per-second counters (frames sent, RMS mean/max, silence fraction).
- A poller logs every transition of held / gated / audible_frames /
  playing-window, so a server-side switch_input_hold is visible.
- turn_started / turn_settled are logged via _track_turn.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import logging
import signal
import sys
import time

sys.path.insert(0, "/home/ztdgtll/pared-tools")

from alsa_audio import AlsaAudio  # noqa: E402
from musegadget import __version__, config, identity, muse_api  # noqa: E402
from musegadget.link_client import DeviceDescription, LinkSession  # noqa: E402
from musegadget.service import DEFAULT_NOISE_HOST  # noqa: E402
from musegadget.voice_call import VoiceCall, pcm_rms, positive_seconds  # noqa: E402

AUTH_KEY = "vm_auth" + "_token"
C_PATH = "/home/ztdgtll/voice-test/test2-C.raw"


def log(msg: str) -> None:
    print(f"[probe {time.strftime('%H:%M:%S')}] {msg}", flush=True)


class LoggingCall(VoiceCall):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._c_file = open(C_PATH, "wb")
        self._sent_frames = 0
        self._sec_frames = 0
        self._sec_rms_sum = 0
        self._sec_rms_max = 0
        self._sec_silent = 0
        self._sec_start = time.monotonic()
        self._last_state = None

    async def _event(self, event):
        etype = event.get("type")
        if etype == "input_audio_buffer.append":
            pcm = base64.b64decode(event["audio"])
            await super()._event(event)  # raises _InputGated if invalidated
            self._c_file.write(pcm)
            self._sent_frames += 1
            rms = int(pcm_rms(pcm))
            self._sec_frames += 1
            self._sec_rms_sum += rms
            self._sec_rms_max = max(self._sec_rms_max, rms)
            if rms < 50:
                self._sec_silent += 1
            now = time.monotonic()
            if now - self._sec_start >= 1.0:
                mean = self._sec_rms_sum // max(1, self._sec_frames)
                log(f"SENT {self._sec_frames} frames rms_mean={mean} "
                    f"rms_max={self._sec_rms_max} silent={self._sec_silent} "
                    f"held={self.held} gated={self._gated()}")
                self._sec_frames = self._sec_rms_sum = self._sec_rms_max = 0
                self._sec_silent = 0
                self._sec_start = now
            return
        if etype != "input_audio_buffer.commit":
            log(f"OUT event {etype}")
        await super()._event(event)

    def _track_turn(self, event) -> bool:
        result = super()._track_turn(event)
        log(f"IN {event.get('type')} tracked={result} "
            f"submission={event.get('submission_id', '?')}")
        return result

    def _confirm(self, event) -> None:
        super()._confirm(event)
        log("session READY (confirmed)")

    async def poll_state(self) -> None:
        while True:
            state = (self.held, self._gated(), self._audible_frames,
                     round(max(0.0, self._playing_until - time.monotonic()), 1))
            if state != self._last_state:
                log(f"STATE held={state[0]} gated={state[1]} "
                    f"audible_frames={state[2]} playing_in={state[3]}s")
                self._last_state = state
            await asyncio.sleep(0.1)


async def run(audio, args) -> str:
    pairing = config.load_json(config.PAIRING_FILE)
    if not pairing or not pairing.get("access_token"):
        raise RuntimeError("not paired")
    ident = identity.load_or_create()
    logging.getLogger("websockets").setLevel(logging.WARNING)
    vms, status = await asyncio.to_thread(
        muse_api.fetch_vms_with_status, pairing["access_token"],
        muse_api.api_root(pairing.get("api_url_v2", "")))
    if status != 200:
        raise ConnectionError("VM lookup failed")
    matches = [vm for vm in vms if vm["vm_id"] == args.vm_id]
    if len(matches) != 1:
        raise ValueError("--vm-id must exactly match one leased VM")
    vm = matches[0]
    session = LinkSession(
        noise_host=pairing.get("noise_host") or DEFAULT_NOISE_HOST,
        vm_id=vm["vm_id"], vm_auth_token=vm.get(AUTH_KEY), app_id=args.app_id,
        register_device=False,
        device=DeviceDescription(ident.node_id, "Muse voice", __version__, {}),
        run_command=lambda *unused: {"ok": False, "error": "unsupported command"},
    )
    stop = asyncio.Event()
    connection = asyncio.create_task(session.run(stop))
    current = asyncio.current_task()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, current.cancel)
    try:
        await session.wait_transport_ready(35)
        call = LoggingCall(
            session, device_id=ident.node_id, thread_id=args.thread_id,
            half_duplex=not args.full_duplex,
            mic_threshold=args.mic_threshold, idle_timeout=args.idle_timeout,
            overall_timeout=args.overall_timeout)
        log(f"call created half_duplex={not args.full_duplex} "
            f"mic_threshold={args.mic_threshold}")
        poller = asyncio.create_task(call.poll_state())
        try:
            return await call.run(audio.frames(), audio, realtime_input=True)
        finally:
            poller.cancel()
    finally:
        stop.set()
        try:
            await asyncio.wait_for(connection, 3)
        except (Exception, asyncio.CancelledError):
            connection.cancel()
            await asyncio.gather(connection, return_exceptions=True)
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(signum)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--vm-id", required=True)
    parser.add_argument("--app-id", default="hatch-web")
    parser.add_argument("--full-duplex", action="store_true")
    parser.add_argument("--mic-threshold", type=int, default=0)
    parser.add_argument("--idle-timeout", type=positive_seconds, default=90)
    parser.add_argument("--overall-timeout", type=positive_seconds, default=600)
    parser.add_argument("--thread-id")
    args = parser.parse_args()
    audio = AlsaAudio()
    try:
        reason = asyncio.run(run(audio, args))
        print(f"Voice call ended: {reason}.", flush=True)
        return 0
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("Voice call stopped.", flush=True)
        return 130
    except Exception as exc:
        print(f"Voice call failed: {type(exc).__name__}: {exc}", flush=True)
        return 1
    finally:
        audio.close()


if __name__ == "__main__":
    raise SystemExit(main())
