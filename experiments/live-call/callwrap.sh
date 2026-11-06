#!/bin/bash
# Call gate wrapper (2026-11-06; hardened same day after Astra's
# review): owns the room while a live call runs. Spawned by the
# listener as its own transient systemd unit on a gate wake, so
# stopping the listener unit cannot kill it.
#
# Hardening vs the first version:
# - flock: only one wrapper may own the room.
# - The 35 min backstop has a FIXED unit name and is cancelled on
#   normal exit — the first version left one armed timer per call,
#   and a stale one could start the listener in the middle of a
#   later call.
# - trap cleanup restores wall + listener however the wrapper dies;
#   TERM is forwarded to the runner first so the SDK closes cleanly.
# - The DSP profile (muxes + ASR path) is SET, not assumed, and the
#   read-backs are logged; command failures are logged, not hidden.
exec >> /home/ztdgtll/voice-test/callwrap.log 2>&1
echo "=== callwrap start $(date -Is) wav=${1:-none}"
export XDG_RUNTIME_DIR=/run/user/$(id -u)

exec 9>/tmp/musez-callwrap.lock
if ! flock -n 9; then
  echo "LOCK held by another wrapper; exiting"
  exit 1
fi

STOPPED=0
RESTORED=0
RUNNER_PID=""
restore() {
  if [ "$RESTORED" = 1 ]; then return; fi
  RESTORED=1
  systemctl --user stop musez-gate-backstop.timer >/dev/null 2>&1
  python3 /home/ztdgtll/pared/setstate.py --manual scene=home \
    avatar=idle title= text="studiolab online" sub= lines=[] \
    >/dev/null 2>&1
  if [ "$STOPPED" = 1 ]; then
    systemctl --user start musez-listener.service
  fi
  echo "restored (listener was stopped=$STOPPED)"
}
on_term() {
  echo "wrapper TERM received; forwarding to runner"
  if [ -n "$RUNNER_PID" ]; then kill -TERM "$RUNNER_PID" 2>/dev/null; fi
}
trap on_term TERM INT
trap restore EXIT

systemctl --user stop musez-listener.service
STOPPED=1
echo "listener stopped rc=$?"

systemd-run --user --on-active=35m \
  --unit=musez-gate-backstop \
  /usr/bin/systemctl --user start musez-listener.service >/dev/null 2>&1 \
  || echo "backstop arm failed (one may already exist)"

# DSP profile: set explicitly (volatile across DSP reboots), then
# log the read-backs as evidence. Volumes: card 2 feature units to
# 0 dB, PipeWire sinks to 1.0.
amixer -c 2 sset PCM,0 60 >/dev/null 2>&1
amixer -c 2 sset PCM,1 60 >/dev/null 2>&1
wpctl set-volume 77 1.0 >/dev/null 2>&1
wpctl set-volume 78 1.0 >/dev/null 2>&1
XVDIR=/home/ztdgtll/xvf3800-repo/host_control/rpi_64bit
(
  cd "$XVDIR" || exit 1
  for kv in "AUDIO_MGR_OP_L 8 0" "AUDIO_MGR_OP_R 7 3" \
            "AEC_ASROUTONOFF 1" "AEC_ASROUTGAIN 1" \
            "AUDIO_MGR_REF_GAIN 100"; do
    if sudo ./xvf_host $kv >/dev/null 2>&1; then
      echo "dsp set ok: $kv"
    else
      echo "dsp set FAILED: $kv"
    fi
  done
  echo "dsp readback OP_L: $(sudo ./xvf_host AUDIO_MGR_OP_L 2>/dev/null | tr -d '\r')"
  echo "dsp readback OP_R: $(sudo ./xvf_host AUDIO_MGR_OP_R 2>/dev/null | tr -d '\r')"
)

# The old listener's arecord dies with its process (broken pipe);
# wait until the capture device is actually free.
for i in $(seq 1 30); do
  pgrep -f "arecord -D plughw:2,0" >/dev/null || break
  sleep 0.5
done
echo "mic free after ${i} checks"

INJ=()
PROB=()
if [ -n "${1:-}" ] && [ -s "$1" ]; then
  INJ=(--inject-file "$1")
else
  # Empty wake capture: probation — the call must earn its life
  # with a real turn within 60 s of READY or it ends itself.
  PROB=(--probation-secs 60)
  echo "empty capture: probation 60s armed"
fi
RUNNER=${CALLWRAP_RUNNER:-/home/ztdgtll/pared-tools/voice_call_room5.py}
if [ -n "${CALLWRAP_FAKE:-}" ]; then
  echo "FAKE runner: sleeping ${CALLWRAP_FAKE} s"
  sleep "${CALLWRAP_FAKE}" &
  RUNNER_PID=$!
  wait "$RUNNER_PID"
  rc=0
else
  sudo /opt/musegadget/venv/bin/python "$RUNNER" \
    --vm-id ${MUSEZ_VM_ID} --full-duplex \
    --idle-timeout 90 --overall-timeout 1800 --pause-hangover 0 \
    "${INJ[@]}" "${PROB[@]}" >> /home/ztdgtll/voice-test/room5.log 2>&1 &
  RUNNER_PID=$!
  wait "$RUNNER_PID"
  rc=$?
fi
RUNNER_PID=""
echo "=== call ended rc=$rc $(date -Is)"
echo "=== callwrap done $(date -Is)"
