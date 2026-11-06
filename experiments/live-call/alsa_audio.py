"""ALSA live audio backend for musegadget VoiceCall.

Mirrors the semantics of musegadget.voice_audio.LiveAudio (bounded queues,
drop-oldest capture, queue cleared when frames() starts, flush drops pending
playback) but talks to the room's hardware through ALSA subprocesses, because
PortAudio enumerates no capture device on this Pi.

Capture: arecord -D plughw:2,0 -c 2 -r 16000 (reSpeaker XVF3800 at its
native rate — the exact recipe the room's listener uses). Python upsamples
16k -> 24k (linear, 3:2) and feeds, per 40 ms block, whichever channel has
the higher RMS; both channels' RMS is logged so the choice is observable.
Playback: pw-cat --playback into PipeWire's default sink (KT USB Audio).
Direct ALSA access to card 3 is impossible in this room: WirePlumber holds
the device permanently (any aplay -D plughw:3,0 fails with "Device or
resource busy"), which is why the first live-call tests were silent.
pw-cat reads its input through libsndfile, so raw PCM is rejected; the
player is fed a streaming WAV (RIFF header with a huge data chunk, then
PCM frames) - verified audible in the room on 2026-11-05. pw-cat must run
as the desktop user with their XDG_RUNTIME_DIR: PipeWire lives in that
session and the call itself runs as root. flush() restarts the player to
drop buffered samples. Player stderr goes to a file, never /dev/null.
"""

from __future__ import annotations

import asyncio
import queue
import struct
import subprocess
import sys
import threading
import time
from collections import deque

from musegadget.voice_call import FRAME_BYTES, FRAME_SAMPLES, SAMPLE_RATE

CAPTURE_DEVICE = "plughw:2,0"
PLAYBACK_DEVICE = "plughw:3,0"
# Far-end reference destination (2026-11-05): the XVF3800's own USB playback
# endpoint (card 2, interface 1, 2ch @ 16 kHz). The chip expects ONE far-end
# reference (AEC_NUM_FARENDS=1) and its AEC never converged in this room
# (AEC_AECCONVERGED=0) because playback went only to the KT DAC: the echo
# canceller worked blind and its residual suppression ate the user's voice.
# Feeding it a copy of everything we play lets it subtract echo properly.
REF_DEVICE = "plughw:2,0"
# Direct ALSA playback to card 2 fails "Device or resource busy" (PipeWire
# holds the endpoint), so the reference goes through PipeWire to the
# XVF3800's own sink, exactly like the room playback route.
REF_TARGET = ("alsa_output.usb-Seeed_Studio_reSpeaker_XVF3800_4-Mic_Array"
              "_114993701262800818-00.analog-stereo")
CAPTURE_RATE = 16000
# Software gain on the sent stream (2026-11-05): the XVF3800's DSP outputs
# can sit ~15 dB lower than the room's earlier baseline (sent-stream floor
# ~65 vs ~340) even with the ALSA capture volume at 100%, and then the
# provider VAD only fires when the user speaks very loudly. x3 lifts normal
# speech back into the range that reliably created turns; peaks clamp.
GAIN = 3
# Fixed capture channel (2026-11-05, XVF3800 routing read via xvf_host):
# ch0 = "Conference" output (AGC up to x32, ducks ~20 dB under playback),
# ch1 = "ASR" output of the same auto-selected beam (no AGC, stable level).
# The original per-block max-RMS pick stitched two differently processed
# signals together and the provider VAD suffered; ASR is the channel the
# vendor intends for speech recognition, so take it deterministically.
CHANNEL = 1  # 0 = Conference, 1 = ASR
_BLOCK_SAMPLES = CAPTURE_RATE * FRAME_SAMPLES // SAMPLE_RATE  # 640 per 40 ms
_BLOCK2_BYTES = _BLOCK_SAMPLES * 2 * 2  # two interleaved channels, PCM16


def _upsample_16k_to_24k(samples) -> bytes:
    """Linear-interpolate 640 samples at 16 kHz to 960 at 24 kHz (3:2)."""
    out = []
    last = len(samples) - 1
    for i in range(FRAME_SAMPLES):
        pos = i * 2
        idx = pos // 3
        frac = (pos % 3) / 3.0
        a = samples[idx]
        b = samples[idx + 1] if idx < last else a
        out.append(int(a + (b - a) * frac))
    return struct.pack(f"<{FRAME_SAMPLES}h", *out)


def _downsample_24k_to_16k_stereo(pcm: bytes) -> bytes:
    """960 samples at 24 kHz mono -> 640 samples at 16 kHz, duplicated to
    stereo (the XVF3800 playback endpoint is 2ch @ 16 kHz)."""
    samples = struct.unpack(f"<{len(pcm) // 2}h", pcm)
    last = len(samples) - 1
    n_out = len(samples) * 2 // 3
    out = []
    for i in range(n_out):
        pos = i * 3
        idx = pos // 2
        frac = (pos % 2) / 2.0
        a = samples[idx]
        b = samples[idx + 1] if idx < last else a
        s = int(a + (b - a) * frac)
        out.append(s)
        out.append(s)
    return struct.pack(f"<{len(out)}h", *out)


def _rms(samples) -> int:
    if not samples:
        return 0
    return int((sum(s * s for s in samples) / len(samples)) ** 0.5)


class AlsaAudio:
    def __init__(self) -> None:
        self._capture: queue.Queue = queue.Queue(maxsize=8)
        self._playback: deque = deque()
        self._playback_bytes = 0
        self._playback_lock = threading.Lock()
        self._stop = threading.Event()
        self._cap_proc = subprocess.Popen(
            ["arecord", "-D", CAPTURE_DEVICE, "-c", "2", "-r", str(CAPTURE_RATE),
             "-f", "S16_LE", "-t", "raw", "-q"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self._cap_thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._cap_thread.start()
        self._play_proc: subprocess.Popen | None = None
        self._hdr_for = None
        self.playback_drops = 0
        self._play_thread = threading.Thread(target=self._playback_loop, daemon=True)
        self._play_thread.start()
        # Warm the player now: the first reply must not pay pw-cat
        # spawn + PipeWire connect + sink unsuspend (test25's stall).
        try:
            _proc = self._ensure_player()
            _proc.stdin.write(self._wav_header())
            _proc.stdin.flush()
            self._hdr_for = _proc
        except (BrokenPipeError, OSError):
            self._play_proc = None
        self._ref: queue.Queue = queue.Queue(maxsize=16)
        self._ref_proc: subprocess.Popen | None = None
        self._ref_thread = threading.Thread(target=self._ref_loop, daemon=True)
        self._ref_thread.start()

    # -- capture ---------------------------------------------------------
    def _capture_loop(self) -> None:
        stream = self._cap_proc.stdout
        frames = 0
        while not self._stop.is_set():
            data = stream.read(_BLOCK2_BYTES)
            if not data or len(data) < _BLOCK2_BYTES:
                return
            samples = struct.unpack(f"<{len(data) // 2}h", data)
            ch0, ch1 = samples[0::2], samples[1::2]
            frames += 1
            if frames % 25 == 0:
                print(f"[alsa] rms ch0={_rms(ch0)} ch1={_rms(ch1)}",
                      file=sys.stderr, flush=True)
            picked = ch1 if CHANNEL == 1 else ch0
            gained = [max(-32768, min(32767, s * GAIN)) for s in picked]
            mono = _upsample_16k_to_24k(gained)
            try:
                self._capture.put_nowait(mono)
            except queue.Full:
                try:
                    self._capture.get_nowait()
                except queue.Empty:
                    pass
                try:
                    self._capture.put_nowait(mono)
                except queue.Full:
                    pass

    async def frames(self):
        self._clear(self._capture)
        while True:
            if self._cap_proc.poll() is not None:
                raise OSError("arecord stopped")
            try:
                yield self._capture.get_nowait()
            except queue.Empty:
                await asyncio.sleep(0.01)

    # -- playback --------------------------------------------------------
    def _ensure_player(self) -> subprocess.Popen:
        if self._play_proc is None or self._play_proc.poll() is not None:
            # PipeWire runs as the desktop user; the call runs as root.
            # pw-cat must drop to that user and use their runtime dir,
            # otherwise pw_context_connect fails with "Host is down".
            import os
            import pwd
            uid = pwd.getpwnam("ztdgtll").pw_uid
            env = dict(os.environ)
            env["XDG_RUNTIME_DIR"] = f"/run/user/{uid}"
            err = open("/home/ztdgtll/voice-test/player.err", "ab")
            self._play_proc = subprocess.Popen(
                ["pw-cat", "--playback", "-"],
                stdin=subprocess.PIPE, stderr=err,
                user="ztdgtll", env=env, cwd="/home/ztdgtll")
        return self._play_proc

    @staticmethod
    def _wav_header() -> bytes:
        return AlsaAudio._wav_header_fmt(SAMPLE_RATE, 1)

    @staticmethod
    def _wav_header_fmt(rate: int, channels: int) -> bytes:
        # Streaming WAV: data chunk declared (almost) endless; libsndfile
        # accepts it from a pipe and PCM follows indefinitely.
        total = 0x7FFFFFFF - 36
        return (b"RIFF" + struct.pack("<I", 36 + total - 8) + b"WAVEfmt " +
                struct.pack("<IHHIIHH", 16, 1, channels, rate,
                            rate * channels * 2, channels * 2, 16) +
                b"data" + struct.pack("<I", total - 8))

    def _playback_loop(self) -> None:
        while not self._stop.is_set():
            with self._playback_lock:
                pcm = self._playback.popleft() if self._playback else None
                if pcm is not None:
                    self._playback_bytes -= len(pcm)
            if pcm is None:
                time.sleep(0.005)
                continue
            try:
                proc = self._ensure_player()
                if proc is not self._hdr_for:
                    proc.stdin.write(self._wav_header())
                    self._hdr_for = proc
                proc.stdin.write(pcm)
                proc.stdin.flush()
                try:
                    self._ref.put_nowait(pcm)
                except queue.Full:
                    pass
            except (BrokenPipeError, OSError):
                self._play_proc = None
                self._hdr_for = None

    # -- far-end reference (XVF3800 AEC) --------------------------------
    def _ensure_ref(self) -> subprocess.Popen:
        if self._ref_proc is None or self._ref_proc.poll() is not None:
            import os
            import pwd
            uid = pwd.getpwnam("ztdgtll").pw_uid
            env = dict(os.environ)
            env["XDG_RUNTIME_DIR"] = f"/run/user/{uid}"
            err = open("/home/ztdgtll/voice-test/ref.err", "ab")
            self._ref_proc = subprocess.Popen(
                ["pw-cat", "--playback", "--target", REF_TARGET, "-"],
                stdin=subprocess.PIPE, stderr=err,
                user="ztdgtll", env=env, cwd="/home/ztdgtll")
        return self._ref_proc

    def _ref_loop(self) -> None:
        hdr_for = None
        while not self._stop.is_set():
            try:
                pcm = self._ref.get(timeout=0.05)
            except queue.Empty:
                continue
            try:
                proc = self._ensure_ref()
                if proc is not hdr_for:
                    proc.stdin.write(self._wav_header_fmt(CAPTURE_RATE, 2))
                    hdr_for = proc
                proc.stdin.write(_downsample_24k_to_16k_stereo(pcm))
                proc.stdin.flush()
            except (BrokenPipeError, OSError):
                self._ref_proc = None
                hdr_for = None

    # Playback robustness (2026-11-05, test25): a stalled pw-cat drain
    # (spawn + PipeWire connect + sink unsuspend at reply start) filled
    # the SDK's 5 s playback buffer and the ValueError killed a healthy
    # call. Rules from that failure: write() NEVER raises for fullness —
    # a dropped frame is a glitch, a raised one is a dead call; the
    # player is pre-spawned warm; flush() clears queues but keeps the
    # pw-cat processes alive so a gate flush never forces a respawn.
    PLAYBACK_FRAMES = 25  # ~1 s cushion

    def write(self, pcm: bytes) -> None:
        if not pcm or len(pcm) > FRAME_BYTES or len(pcm) % 2:
            raise ValueError("playback needs 1-960 PCM16 samples")
        if self._cap_proc.poll() is not None:
            raise OSError("audio device stopped")
        with self._playback_lock:
            if self._playback_bytes + len(pcm) > self.PLAYBACK_FRAMES * FRAME_BYTES:
                self.playback_drops += 1
                return
            self._playback.append(pcm)
            self._playback_bytes += len(pcm)

    def flush(self) -> None:
        with self._playback_lock:
            self._playback.clear()
            self._playback_bytes = 0
        self._clear(self._ref)

    # -- lifecycle -------------------------------------------------------
    @staticmethod
    def _clear(q) -> None:
        while True:
            try:
                q.get_nowait()
            except queue.Empty:
                return

    def close(self) -> None:
        self._stop.set()
        for proc in (self._cap_proc, self._play_proc, self._ref_proc):
            if proc is not None:
                try:
                    proc.terminate()
                except OSError:
                    pass
