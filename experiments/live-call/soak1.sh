#!/bin/bash
# Soak driver (2026-11-06): starts a gate call via callwrap.sh (no
# initial injection), then drops INJECT triggers every 75 s x10 so
# the call gets ~10 real turns with real playback. Replies play at
# low sink volume. Restores volume at the end. Detached; progress
# in soak1.log, completion flag soak1.done.
exec >> /home/ztdgtll/voice-test/soak1.log 2>&1
echo "=== soak start $(date -Is)"
export XDG_RUNTIME_DIR=/run/user/$(id -u)
rm -f /home/ztdgtll/voice-test/soak1.done

systemd-run --user --unit=musez-soak1 --collect \
  /home/ztdgtll/pared-tools/callwrap.sh >/dev/null 2>&1
echo "callwrap launched"

# Let the call set up (DSP profile + READY), then drop the sink
# volume so the soak replies are faint in the room.
sleep 30
wpctl set-volume 77 0.12 >/dev/null 2>&1
echo "sink lowered $(date -Is)"

for i in $(seq 1 10); do
  touch /home/ztdgtll/voice-test/INJECT
  echo "trigger $i $(date -Is)"
  sleep 75
done

# Wait for the call to end on its own (idle timeout after the
# last reply), at most ~6 extra minutes.
for i in $(seq 1 72); do
  st=$(systemctl --user is-active musez-soak1 2>/dev/null)
  [ "$st" != "active" ] && break
  sleep 5
done
echo "callwrap state: $(systemctl --user is-active musez-soak1 2>/dev/null) $(date -Is)"

wpctl set-volume 77 1.0 >/dev/null 2>&1
echo "sink restored"
touch /home/ztdgtll/voice-test/soak1.done
echo "=== soak done $(date -Is)"
