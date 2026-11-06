"""Realtime Muse voice call through the studio room's ALSA audio path.

Same session/call construction as `musegadget voice-call --live`, with the
PortAudio LiveAudio backend replaced by AlsaAudio (arecord/aplay on the
room's reSpeaker + KT USB devices). Run with the musegadget venv python,
as root (pairing files live under /var/lib/musegadget).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

sys.path.insert(0, "/home/ztdgtll/pared-tools")

from alsa_audio import AlsaAudio  # noqa: E402
from musegadget import __version__, config, identity, muse_api  # noqa: E402
from musegadget.link_client import DeviceDescription, LinkSession  # noqa: E402
from musegadget.service import DEFAULT_NOISE_HOST  # noqa: E402
from musegadget.voice_call import VoiceCall, positive_seconds  # noqa: E402

AUTH_KEY = "vm_auth" + "_token"


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
        call = VoiceCall(
            session, device_id=ident.node_id, thread_id=args.thread_id,
            half_duplex=not args.full_duplex,
            mic_threshold=args.mic_threshold, idle_timeout=args.idle_timeout,
            overall_timeout=args.overall_timeout)
        return await call.run(audio.frames(), audio, realtime_input=True)
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
    parser.add_argument("--idle-timeout", type=positive_seconds, default=60)
    parser.add_argument("--overall-timeout", type=positive_seconds, default=900)
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
    except Exception as exc:  # keep diagnostics credential-free, like the CLI
        print(f"Voice call failed: {type(exc).__name__}: {exc}", flush=True)
        return 1
    finally:
        audio.close()


if __name__ == "__main__":
    raise SystemExit(main())
