import subprocess, time, os, sys, glob, random, json, re, unicodedata, difflib, threading
from collections import Counter, deque
import numpy as np
from openwakeword.model import Model
from faster_whisper import WhisperModel
from scipy.io import wavfile

HOME = "/home/ztdgtll"
TOOLS = HOME + "/pared-tools"
# 2026-10-17: conversation mode removed by Javier decision - every turn starts with the wake word; await_followup() is kept below but gated off.
FOLLOWUP_ENABLED = False
# Call gate (2026-11-06): when the flag file exists, a wake word
# opens a live voice call (see run_gate) instead of a bridge turn.
# Removing the flag reverts to the classic turn system on the next
# wake; the gate code stays harmless.
CALLGATE_FLAG = TOOLS + "/callgate.enabled"
CALLWRAP = TOOLS + "/callwrap.sh"
GATE_WAV24 = HOME + "/voice-test/gate-command24.wav"
GATE_EXIT = -1.0

def callgate_enabled():
    return os.path.exists(CALLGATE_FLAG)
LOGF = open(TOOLS + "/listener.log", "a", buffering=1)
def log(m):
    LOGF.write(time.strftime("%H:%M:%S ") + m + "\n")

XV = ["sudo", "-n", HOME + "/voice-venv/bin/python", HOME + "/xvf3800-repo/python_control/xvf_host.py"]
def led_setup():
    try:
        subprocess.run(XV + ["LED_DOA_COLOR", "--values", "0", "16777215"], capture_output=True, timeout=15)
    except Exception as e:
        log("LED setup error: %r" % e)
def led_on():
    try:
        subprocess.run(XV + ["LED_EFFECT", "--values", "4"], capture_output=True, timeout=10)
    except Exception as e:
        log("LED error: %r" % e)
def led_off():
    try:
        subprocess.run(XV + ["LED_EFFECT", "--values", "0"], capture_output=True, timeout=10)
    except Exception as e:
        log("LED error: %r" % e)
# The flash script sleeps 0.7 s INSIDE its run before its final
# LED_EFFECT 4: if a cleanup path called led_off() during that sleep,
# the script's late write re-lit the ring after cleanup (Astra v2
# §7). Keep the process handle so cleanup can kill a pending flash
# before the final led_off().
_flash_proc = [None]

def led_flash_start():
    try:
        _flash_proc[0] = subprocess.Popen(["bash", TOOLS + "/ledflash.sh"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        log("FLASH_FAIL %r" % e)

def led_flash_stop():
    p = _flash_proc[0]
    _flash_proc[0] = None
    if p is not None and p.poll() is None:
        try:
            p.terminate()
        except Exception:
            pass
def led_flash2_start():
    try:
        subprocess.Popen(["bash", TOOLS + "/ledflash2.sh"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        log("FLASH_FAIL %r" % e)

# wall() used to run setstate.py with no timeout and no return-code
# check: a wedged writer (or a contended state lock) could park the
# whole turn forever and the failure was invisible (Astra v2 §2).
# A wall write is best-effort decoration for the turn — log and move
# on, never hang on it.
WALL_TIMEOUT_S = 10

def wall(*args):
    try:
        r = subprocess.run(["python3", HOME + "/pared/setstate.py", *args],
                           capture_output=True, timeout=WALL_TIMEOUT_S)
        if r.returncode == 4:
            # Batch B: setstate exit 4 = the guard/sticky/cleanup
            # rules rejected the write (a stale or non-owner write
            # landing nowhere is the system WORKING, not a failure).
            log("WALL_STALE_DROPPED args=%s" % " ".join(args)[:80])
        elif r.returncode != 0:
            log("WALL_FAIL rc=%d args=%s" % (r.returncode, " ".join(args)[:80]))
    except subprocess.TimeoutExpired:
        log("WALL_FAIL timeout args=%s" % " ".join(args)[:80])
    except Exception as e:
        log("WALL_FAIL %s %r" % (type(e).__name__, e))

# Turn-tagged wall state (phase 2, 2026-10-18): every state this
# process writes for a turn carries the turn's id plus a phase rank,
# and setstate.py rejects a write whose (turn, phase) is older than
# what the wall already shows — a slow THINKING from a finished turn
# can no longer overwrite a newer answer or home (Astra review §3).
# Phases: 1 listening, 2 processing, 3 thinking, 4 answer (sayseq,
# which mints its own later turn id), 5 home (the turn's own close).
PH_LISTENING, PH_PROCESSING, PH_THINKING, PH_ANSWER, PH_HOME = 1, 2, 3, 4, 5
_active_turn = [0]

def wall_turn(phase, *args):
    if _active_turn[0] <= 0:
        wall(*args)
        return
    wall("--turn=%d" % _active_turn[0], "--phase=%d" % phase, *args)

def wall_home():
    # Batch B: the turn's close is an ownership-scoped cleanup
    # (--expect-turn) — it lands only while THIS turn still owns the
    # wall. It can no longer erase a newer turn's state or a sticky
    # result the agent published (a no-voice wake used to be able to
    # wipe a result simply by ending; Astra v2 §6).
    if _active_turn[0] > 0:
        wall("--turn=%d" % _active_turn[0], "--phase=%d" % PH_HOME,
             "--expect-turn=%d" % _active_turn[0],
             "scene=home", "avatar=idle", "title=", "lines=[]",
             "text=studiolab online", "expiresAt=null", "wave=false")
    else:
        wall("scene=home", "avatar=idle", "title=", "lines=[]",
             "text=studiolab online", "expiresAt=null", "wave=false")


# Turn minting (batch B): fresh wall-turn ids are reserved under
# the same flock + floor sidecar setstate.py uses, in-process (a
# subprocess per wake would add ~100 ms to every turn). Two mints
# in the same millisecond — ours vs sayseq's --mint-turn vs the
# agent's — can neither collide nor go backwards: the candidate is
# max(now_ms, file turn + 1, floor + 1, last_mint + 1) and the
# reservation is written back before the lock is released.
def mint_turn():
    try:
        import fcntl
        spath = HOME + "/pared/state.json"
        lock = open(spath + ".lock", "a+")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
            f_turn = f_floor = last_mint = 0
            side = {}
            try:
                with open(spath) as f:
                    st = json.load(f)
                if isinstance(st, dict):
                    f_turn = int(st.get("turn") or 0)
                    f_floor = int(st.get("floor") or 0)
            except Exception:
                pass
            try:
                with open(spath + ".floor.json") as f:
                    side = json.load(f)
                if isinstance(side, dict):
                    f_turn = max(f_turn, int(side.get("turn") or 0))
                    f_floor = max(f_floor, int(side.get("floor") or 0))
                    last_mint = int(side.get("last_mint") or 0)
                else:
                    side = {}
            except Exception:
                side = {}
            cand = max(int(time.time() * 1000), f_turn + 1,
                       f_floor + 1, last_mint + 1)
            side["last_mint"] = cand
            tmp = spath + ".floor.json.tmp-%d" % os.getpid()
            with open(tmp, "w") as f:
                json.dump(side, f)
            os.rename(tmp, spath + ".floor.json")
            return cand
        finally:
            try:
                fcntl.flock(lock, fcntl.LOCK_UN)
            finally:
                lock.close()
    except Exception as e:
        log("MINT_FAIL %r" % e)
        return int(time.time() * 1000)

# Voice waveform feed for the wall (Javier's idea, 2026-10-17): while
# any capture runs, keep the last ~48 chunk RMS values (channel 0)
# and publish them atomically to ~/pared/levels.json (tmp + rename —
# never a partial file) at most every ~100 ms. The page animates
# the logo with them only while state.wave is true. Levels
# are normalized 0..1 with a room-tone floor and a soft curve,
# calibrated on Javier's real samples (speech RMS p90 ~2300). The
# page animates the Muse squiggle with them while state.wave is
# true — on ANY speaking turn, wake or follow-up (2026-10-17,
# extended from follow-up only).
LEVELS_PATH = HOME + "/pared/levels.json"
LEVELS_N = 48
_levels_hist = []
_levels_last = [0.0]

def _levels_norm(rms):
    # Recalibrated 2026-10-26: the live capture chain now delivers far
    # quieter audio than the Oct-17 DSP test captures this scale was
    # built for (measured tonight: speech windows p90 RMS ~40, peaks
    # ~145, vs test-capture peaks 5000+). With floor 200 every value
    # normalized to 0.0 and the wall wave rendered flat. New floor/span
    # fit the measured live range; if the chain goes hot again the
    # min(1.0, ...) clamp saturates the wave instead of breaking it.
    v = max(0.0, rms - 5.0) / 120.0
    return round(min(1.0, v ** 0.7), 3)

def levels_write(active, force=False):
    now = time.time()
    if active and not force and now - _levels_last[0] < 0.1:
        return
    _levels_last[0] = now
    try:
        payload = {"levels": [_levels_norm(v) for v in _levels_hist[-LEVELS_N:]],
                   "active": bool(active), "ts": int(now * 1000)}
        tmp = LEVELS_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(payload, f)
        os.rename(tmp, LEVELS_PATH)
    except Exception:
        pass

def levels_push(rms):
    _levels_hist.append(rms)
    if len(_levels_hist) > LEVELS_N * 4:
        del _levels_hist[:LEVELS_N * 2]
    levels_write(True)

def levels_idle():
    levels_write(False, force=True)

def gated():
    return os.path.exists(TOOLS + "/speaking.flag") or os.path.exists(TOOLS + "/listener-muted.flag")

class TurnMuted(Exception):
    """Raised inside a capture when the manual mute lands mid-turn."""

class CaptureAborted(Exception):
    """A capture invalidated mid-flight (batch A capture hygiene):
    EOF after speech was heard (the mic died — the fragment is NOT
    a command), a ChunkFeed discontinuity (dropped chunks mean the
    recorded frames have a hole), or a playback session starting
    while Javier is still speaking (the capture would record the
    answer, not him). All three abort the turn: nothing is sent."""
    TAGS = {"EOF": "CAPTURE_ABORTED_EOF",
            "DISCONTINUITY": "CAPTURE_DISCONTINUITY_ABORT",
            "PLAYBACK": "CAPTURE_ABORTED_PLAYBACK"}

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason
        self.tag = self.TAGS.get(reason, "CAPTURE_ABORTED_" + reason)

# Orphan speaking.flag healing (Astra review 2026-10-18 batch 1
# fix 3; rebased in batch A on the playback layer). gated() trusts
# the flag's existence forever, so a player dying mid-playback used
# to gate the mic indefinitely and startup never reconciled it.
# Since batch A the flag is a product of a playgate session, and
# the session's owner record (playback-active.json) is the primary
# liveness evidence: a record whose owner pid is alive means the
# gate is legitimately held — even in the silent gap between two
# sentences, when momentarily no mpv exists (the old healer could
# clear the gate mid-answer there). pgrep is only a secondary
# witness and its exit codes are honored exactly: 0 = some mpv
# alive (keep), 1 = no mpv (the ONLY code that means absence),
# >= 2 or an exception = unknown (log HEALER_UNKNOWN, keep).
# A flag is cleared only when it is OLD (> 60 s: protects the
# create->spawn gap and a legitimate playback spanning a listener
# restart), no live session record exists and no mpv is alive.
# listener-muted.flag is a manual persistent mute, NEVER cleared.
def _pid_alive(pid):
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False

def reconcile_speaking_flag():
    flag = TOOLS + "/speaking.flag"
    try:
        age = time.time() - os.path.getmtime(flag)
    except OSError:
        return
    if age <= 60:
        return
    rec = None
    rec_age = 0.0
    rec_path = TOOLS + "/playback-active.json"
    try:
        with open(rec_path) as f:
            rec = json.load(f)
        rec_age = time.time() - float(rec.get("started_ts") or 0)
    except Exception:
        rec = None
    # A record older than an hour is not credible liveness evidence
    # (no session lasts that long; its pid may have been recycled).
    if rec is not None and rec_age <= 3600 and _pid_alive(rec.get("pid")):
        return
    try:
        r = subprocess.run(["pgrep", "-x", "mpv"], capture_output=True, timeout=5)
    except Exception as e:
        log("HEALER_UNKNOWN pgrep raised %r" % e)
        return
    if r.returncode == 0:
        return
    if r.returncode != 1:
        log("HEALER_UNKNOWN pgrep rc=%d" % r.returncode)
        return
    try:
        os.remove(flag)
        log("FLAG_ORPHAN_CLEARED age=%.0fs" % age)
    except OSError:
        pass
    if rec is not None and rec_age > 60:
        try:
            os.remove(rec_path)
            log("PLAYBACK_RECORD_CLEARED owner=%s age=%.0fs" % (rec.get("owner"), rec_age))
        except OSError:
            pass

# Pre-roll (2026-10-17 polish): recordings used to start AT detection,
# which ate sentence heads (two turns today arrived as just "Es.")
# and left phantom sources unidentifiable (no pre-trigger audio).
# Keep a rolling buffer of the last ~1.5 s of mic chunks, accumulated
# under the SAME gates as wake-word prediction input (not while
# speaking/muted, not during cooldown). At a turn's capture start the
# buffer is prepended to the recording for the SAVED last-command.wav
# (forensics) only. Since the 2026-10-17 night fix the STT engines get
# the recorded frames WITHOUT the pre-roll: forensics that night proved
# the pre-roll holds the clipped, max-volume tail of the wake phrase
# (the detector fires ~0.8 s after he starts speaking, peak 32768),
# every engine transcribed the combined clip worse, and Qwen even
# leaked the burst into the sent command ("Hey news."). The real
# command starts ~1.68 s after the pre-roll starts, so trimming at
# the boundary loses no syllables. A leading wake phrase in the
# transcript is still stripped before sending (harmless second net).
PREROLL_CHUNKS = 19  # 19 chunks x 80 ms = 1.52 s at 16 kHz
_preroll = deque(maxlen=PREROLL_CHUNKS)

def preroll_push(chunk):
    _preroll.append(chunk)

def preroll_snapshot():
    if not _preroll:
        return None
    pre = np.concatenate(list(_preroll))
    _preroll.clear()
    return pre

_WAKE_PREFIX = re.compile(r"^\s*(hey|ey|hei)\s*[,.]?\s*(muse|mius)\b[.,!?\s]*", re.IGNORECASE)

def strip_wake_prefix(text):
    prev, t = None, text
    while prev != t:
        prev = t
        t = _WAKE_PREFIX.sub("", t).lstrip(" ,.")
    return t.strip()

# Echo guard (2026-10-17 polish): a follow-up window once captured
# MuseZ's OWN spoken reply bleeding back through the room and sent
# it as if it were Javier. sayseq persists the exact spoken text to
# last-spoken.txt (sidecar shipped by respond2.sh); before sending a
# FOLLOW-UP turn, drop it silently when it matches that text.
LAST_SPOKEN = TOOLS + "/last-spoken.txt"

def _norm_text(t):
    t = unicodedata.normalize("NFD", t.lower())
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()

def is_echo_of_last_spoken(text):
    try:
        if time.time() - os.path.getmtime(LAST_SPOKEN) > 180:
            return False
        with open(LAST_SPOKEN) as f:
            raw = f.read()
    except OSError:
        return False
    cand = _norm_text(text)
    if len(cand) < 8:
        return False
    full = _norm_text(raw)
    if not full:
        return False
    if cand in full or full in cand:
        return True
    if difflib.SequenceMatcher(None, cand, full).ratio() >= 0.75:
        return True
    for line in raw.splitlines():
        s = _norm_text(line)
        if s and difflib.SequenceMatcher(None, cand, s).ratio() >= 0.75:
            return True
    return False

led_setup()
led_off()
ww = Model(wakeword_model_paths=[TOOLS + "/hey_muse.onnx"])
stt = WhisperModel("small", device="cpu", compute_type="int8", cpu_threads=4, num_workers=1)
sys.path.insert(0, TOOLS)
import playgate  # the single playback layer (batch A, 2026-10-18)
qwen = None
try:
    from qwen3_handler import Qwen3AsrTranscriber
    qwen = Qwen3AsrTranscriber(TOOLS + "/qwen-model", cache_dir=TOOLS + "/qwen-cache", cpu_threads=4, device="cpu")
    log("QWEN_READY")
except Exception as e:
    log("QWEN_LOAD_FAIL %r" % e)
vad = None
try:
    from pysilero_vad import SileroVoiceActivityDetector
    vad = SileroVoiceActivityDetector()
    log("VAD_READY")
except Exception as e:
    log("VAD_LOAD_FAIL %r" % e)
FILLERS = sorted(glob.glob(TOOLS + "/fillers/*.ogg"))
last_filler = [None]
log("FILLERS_READY n=%d" % len(FILLERS))
# English fillers (2026-11-01 night): the same pre-recorded early
# ack in English — same Warm voice, same format — for turns the
# STT switch transcribed in English. A bridge turn is acknowledged
# from THIS set when its language is English (see process_audio).
FILLERS_EN = sorted(glob.glob(TOOLS + "/fillers-en/*.ogg"))
last_filler_en = [None]
log("FILLERS_EN_READY n=%d" % len(FILLERS_EN))
def play_filler(lang="es"):
    # History: the filler originally created speaking.flag itself and
    # deleted it in a finally, which could strip the gate from under
    # a real answer (phase 2 removed that: the flag got ONE owner,
    # the sequencer). Batch A (2026-10-18) generalizes the owner into
    # the playback layer: EVERY audio output — answers, say.sh and
    # this filler — is a playgate session holding the playback flock,
    # and the flag is a product of the session. The filler is
    # disposable by design: it never waits for the gate. If an
    # answer is playing (or starting) it is skipped, and during OFF
    # hours without a fresh live-turn token the layer refuses it —
    # a filler minted by a just-captured turn carries that token, so
    # a deliberate night turn still gets its acknowledgement.
    #
    # `lang` selects the recording set ("en" -> fillers-en, anything
    # else -> the Spanish set, exactly as before this parameter
    # existed; the classic caller passes nothing). Returns True only
    # when the filler actually played: the bridge branch records
    # that fact on the queue entry so the bridge suppresses its own
    # TTS acknowledgement — exactly one ack per turn. A missing set
    # (no English recordings) returns False and the bridge's own
    # ack stays as the backstop — never a wrong-language filler.
    if lang == "en":
        pool, last = FILLERS_EN, last_filler_en
    else:
        pool, last = FILLERS, last_filler
    if not pool:
        return False
    choices = [f for f in pool if f != last[0]] or pool
    pick = random.choice(choices)
    last[0] = pick
    sess = playgate.PlaybackSession("filler-%d" % int(time.time() * 1000), "filler", wait=0.0)
    status = sess.acquire()
    if status == "busy":
        log("FILLER_SKIPPED_BUSY")
        return False
    if status == "suppressed":
        log("FILLER_SKIPPED_OFF")
        return False
    if status != "ok":
        log("FILLER_SKIPPED %s" % status)
        return False
    try:
        subprocess.run(["mpv", "--volume=90", "--really-quiet", TOOLS + "/preroll2.ogg", TOOLS + "/preroll2.ogg", TOOLS + "/preroll2.ogg", pick], capture_output=True, timeout=25)
        log("FILLER %s" % pick.split("/")[-1])
        return True
    except Exception as e:
        log("FILLER_FAIL %r" % e)
        return False
    finally:
        sess.release()
log("MODELS_READY")

# Continuous mic drain (phase 2, 2026-10-18): a reader thread pulls
# arecord's stdout NON-STOP into a bounded, timestamped deque, so the
# pipe can never back up while the main thread is busy (STT, the SDK
# send, the filler). Before this, processing blocked every read and
# the audio queued in the pipe was consumed afterwards as if it were
# live — including the tail of MuseZ's own reply (Astra review §3).
# The consumer discards what it pops while gated; purge_now() drops
# everything queued across a busy/gated boundary, so the detector
# only ever sees fresh audio. arecord EOF stays fatal: read() then
# returns None and the main loop exits for systemd to restart us.
CH = 1280
FEED_MAX_CHUNKS = 375  # 375 chunks x 80 ms = 30 s of audio

class ChunkFeed:
    """Timestamped bounded chunk queue fed by a daemon reader thread.

    read_raw(n) must return exactly n bytes, or fewer/None at EOF
    (proc.stdout.read semantics). Chunks are mono int16 arrays."""

    def __init__(self, read_raw, chunk_bytes, log_fn, max_chunks=FEED_MAX_CHUNKS):
        self._read_raw = read_raw
        self._n = chunk_bytes
        self._log = log_fn
        self._dq = deque(maxlen=max_chunks)
        self._cond = threading.Condition()
        self.eof = False
        self.reader_error = None
        self.dropped = 0
        self.last_read_ts = 0.0
        self._last_drop_log = 0.0

    def _run(self):
        # Exception boundary (batch A, Astra v2 §2): this loop used to
        # run naked — ANY error (a read OSError, a numpy failure, even
        # a logging hiccup) killed ONLY the reader thread: eof stayed
        # False and the consumer waited on the condition forever, a
        # live but permanently deaf service that Restart=always could
        # not see. Now a failure is an explicit channel: log
        # READER_DEAD, set reader_error + eof, wake everyone, and let
        # the main loop exit so systemd restarts a fresh listener.
        try:
            while True:
                raw = self._read_raw(self._n)
                if raw is None or len(raw) < self._n:
                    with self._cond:
                        self.eof = True
                        self._cond.notify_all()
                    return
                chunk = np.frombuffer(raw, dtype=np.int16).reshape(-1, 2)[:, 0].copy()
                ts = time.monotonic()
                with self._cond:
                    if len(self._dq) >= self._dq.maxlen:
                        self.dropped += 1
                        now = time.time()
                        if now - self._last_drop_log >= 60.0:
                            self._last_drop_log = now
                            self._log("DROPPED_CHUNKS total=%d" % self.dropped)
                    self._dq.append((ts, chunk))
                    self._cond.notify()
        except Exception as e:
            try:
                self._log("READER_DEAD %s: %r" % (type(e).__name__, e))
            except Exception:
                pass
            with self._cond:
                self.reader_error = e
                self.eof = True
                self._cond.notify_all()
            return

    def start(self):
        t = threading.Thread(target=self._run, daemon=True)
        t.start()
        return t

    def read_item(self):
        """Oldest (ts, chunk), blocking; None at EOF or reader death."""
        with self._cond:
            while True:
                if self.reader_error is not None:
                    return None
                if self._dq:
                    ts, chunk = self._dq.popleft()
                    self.last_read_ts = ts
                    return ts, chunk
                if self.eof:
                    return None
                self._cond.wait(timeout=0.5)

    def depth(self):
        with self._cond:
            return len(self._dq)

    def purge_now(self):
        """Drop everything queued; returns how many chunks went."""
        with self._cond:
            n = len(self._dq)
            self._dq.clear()
            return n

proc = subprocess.Popen(["arecord", "-D", "plughw:2,0", "-c", "2", "-r", "16000", "-f", "S16_LE", "-t", "raw", "-q"], stdout=subprocess.PIPE)
feed = ChunkFeed(proc.stdout.read, CH * 4, log)
feed.start()
log("LISTENING")
levels_idle()

# Progress supervision (batch A, Astra v2 §2): Restart=always only
# sees a DEAD process; a listener whose main thread wedges inside a
# native inference or a stuck subprocess stays "active" forever.
# The listener therefore stamps _progress_ts at every chunk consumed
# and at every turn-phase boundary, and a supervisor thread pings the
# systemd watchdog (WATCHDOG=1 via $NOTIFY_SOCKET; the unit sets
# WatchdogSec=120 + NotifyAccess=main) ONLY while progress is fresh
# (< 90 s). Thresholds vs the measured worst healthy phases: capture
# touches per chunk; STT is bracketed by touches (worst long-clip
# inference ~45 s); the SDK send is bracketed too (its own timeout
# is 60 s); the filler is bracketed by process_audio's return. The
# largest possible gap between touches in a healthy system is ~60 s
# < 90 s, so a healthy listener is never watchdog-killed, while a
# wedged one stops pinging and is restarted within ~2 minutes.
# Belt and braces: the supervisor also rewrites listener-progress.txt
# on the same freshness condition; health.sh fails if that file goes
# stale (> 150 s), which keeps the signal even if sd_notify plumbing
# ever breaks.
PROGRESS_FILE = TOOLS + "/listener-progress.txt"
_progress_ts = [time.monotonic()]

def touch_progress():
    _progress_ts[0] = time.monotonic()

def _sd_notify(msg):
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return False
    try:
        import socket
        if addr.startswith("@"):
            addr = "\0" + addr[1:]
        s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            s.sendto(msg.encode(), addr)
        finally:
            s.close()
        return True
    except Exception:
        return False

def _supervisor():
    while True:
        time.sleep(20)
        fresh = (time.monotonic() - _progress_ts[0]) < 90.0
        if not fresh:
            continue
        try:
            with open(PROGRESS_FILE, "w") as f:
                f.write(str(time.time()))
        except OSError:
            pass
        _sd_notify("WATCHDOG=1")

threading.Thread(target=_supervisor, daemon=True).start()

def read_chunk():
    item = feed.read_item()
    if item is None:
        return None
    return item[1]

def capture_command(no_speech_limit):
    """Record one command with Silero endpointing. Returns int16 mono
    audio array, or None if no speech was heard. The wall is NOT touched:
    during listen/think the wall stays on the home screen (Javier
    2026-10-17: the THINKING screen read as a confusing listening state)."""
    frames = []
    silence_run = 0
    speech_seen = False
    vad_buf = np.array([], dtype=np.int16)
    vad_silence = 0.0
    vad_speech = False
    vad_dead = False
    vad_err_logged = False
    if vad is not None:
        try:
            vad.reset()
        except Exception:
            pass
    heard = False
    led_since = time.time()
    led_is_on = True
    t_start = time.time()
    dropped0 = feed.dropped
    eof_hit = False
    while time.time() - t_start < 20:
        c = read_chunk()
        if c is None:
            eof_hit = True
            break
        touch_progress()
        if os.path.exists(TOOLS + "/listener-muted.flag"):
            raise TurnMuted()
        if feed.dropped != dropped0:
            raise CaptureAborted("DISCONTINUITY")
        if os.path.exists(TOOLS + "/playback-active.json"):
            raise CaptureAborted("PLAYBACK")
        frames.append(c)
        rms = float(np.sqrt(np.mean(c.astype(np.float64) ** 2)))
        levels_push(rms)
        if rms > 500:
            speech_seen = True
            silence_run = 0
        else:
            silence_run += 1
        if vad is not None and not vad_dead:
            vad_buf = np.concatenate([vad_buf, c])
            while len(vad_buf) >= 512:
                win = vad_buf[:512]
                vad_buf = vad_buf[512:]
                try:
                    p = float(vad(win.astype(np.int16).tobytes()))
                except Exception as e:
                    # A VAD failure must not masquerade as silence:
                    # the old p=0.0 swallow accumulated fake silence
                    # after any positive detection and ended captures
                    # early, with the energy fallback disabled once
                    # vad_speech was true. Log once, kill VAD for the
                    # rest of THIS capture, and let energy endpointing
                    # decide (Astra review 2026-10-18, batch 1 fix 4).
                    if not vad_err_logged:
                        log("VAD_ERROR %r - energy endpointing for this capture" % e)
                        vad_err_logged = True
                    vad_dead = True
                    break
                if p >= 0.5:
                    vad_speech = True
                    vad_silence = 0.0
                else:
                    vad_silence += 512 / 16000.0
        heard = speech_seen or vad_speech
        if led_is_on and heard and time.time() - led_since > 2.0:
            led_flash_stop()
            led_off()
            led_is_on = False
        if heard and ((vad_speech and not vad_dead and vad_silence >= 1.0) or ((not vad_speech or vad_dead) and silence_run * CH / 16000.0 >= 2.0)):
            break
        if not heard and time.time() - t_start > no_speech_limit:
            break
    if eof_hit:
        # The tail below is skipped on this path too: run_turn's
        # CaptureAborted handler redoes levels/LED and the wall.
        if heard:
            raise CaptureAborted("EOF")
        return None
    levels_idle()
    if led_is_on:
        led_flash_stop()
        led_off()
    if not heard:
        return None
    return np.concatenate(frames)


# --- Bridge destination (hackathon, 2026-10-31) -------------------
# When the API switch (brain_switch, deployed beside this file)
# reads bridge mode, a mic turn is QUEUED for the bridge daemon
# (~/pared-tools/bridge-queue.jsonl; the daemon runs the live turn
# with this same turn id) instead of being sent to the chat. The
# classic send below stays untouched as the fallback: switch not in
# bridge mode, switch module missing, or the queue write failing
# all land on the classic path - a queue problem never loses a turn.
try:
    import brain_switch as _brain_switch
except Exception:
    _brain_switch = None


def _brain_bridge():
    if _brain_switch is None:
        return False
    try:
        return bool(_brain_switch.bridge_enabled())
    except Exception:
        return False


def _queue_bridge_turn(base, text, filler=False):
    try:
        entry = {"turn": _active_turn[0], "base": base, "text": text,
                 "ts": time.time(), "lang": _stt_lang()}
        # "filler" rides only when this turn's early ack already
        # sounded (the pre-recorded filler, in the turn's language):
        # the bridge reads it and suppresses its own TTS ack, so the
        # room hears exactly one acknowledgement. Entries without it
        # (older lines, hand-fed lines) keep the bridge ack.
        if filler:
            entry["filler"] = True
        with open(TOOLS + "/bridge-queue.jsonl", "a") as _qf:
            _qf.write(json.dumps(entry) + "\n")
            _qf.flush()
            os.fsync(_qf.fileno())
        return True
    except Exception:
        return False


def _register_sent(base, text):
    """Watchdog registration + THINKING wall state, shared by the
    classic send and the bridge queue: both destinations owe the
    room the same evidence contract (batch D/E semantics)."""
    # Batch D (2026-10-23): register the turn with the response
    # watchdog (respwatch.py). It verifies LOCAL evidence that this
    # turn got its spoken answer and escalates if none lands. This
    # append must never break the send path: fail silent.
    try:
        with open(TOOLS + "/awaiting.jsonl", "a") as _wf:
            # 2026-11-02: "lang" rides along so the watchdog's
            # canned lines can follow the turn's language.
            _wf.write(json.dumps({"turn": _active_turn[0], "ts": time.time(),
                                  "low": base != "studio-mic",
                                  "lang": _stt_lang(),
                                  "text": text[:160]}) + "\n")
    except Exception:
        pass
    # Batch E: with the turn dispatched, the agent is now working
    # on it - the wall says so (avatar/text WORKING; the phase stays
    # the rank-3 slot so the guard's ordering is unchanged).
    wall_turn(PH_THINKING, "scene=home", "avatar=working", "title=", "lines=[]", "text=WORKING", "live=false", "wave=false", "expiresAt=" + time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 90)))


def _stt_lang():
    """STT language for this turn, from the idioma switch file
    (2026-11-01): ~/pared-tools/stt-lang holds "en" or "es". Read
    fresh on EVERY turn, so flipping the file takes effect on the
    next command with no listener restart. A missing file or any
    unrecognized content fails closed to "es": Spanish is the room
    default and daily use stays exactly as it was."""
    try:
        with open(TOOLS + "/stt-lang", "r") as f:
            if f.read().strip().lower() == "en":
                return "en"
    except Exception:
        pass
    return "es"


def process_audio(audio_i16, base, stt_i16=None):
    """STT (hybrid) + send via the SDK. Returns (sent_ok, is_normal).

    `base` is the message class ("studio-mic", "studio-mic-low" or
    "studio-mic-followup"); the wire prefix gains this turn's id —
    "[studio-mic t=<turn>] " (batch B) — so the agent can thread
    the ORIGINAL request identity into its spoken reply's manifest
    and the answer's wall writes stay causally owned by this turn.

    audio_i16 is the FULL clip (pre-roll included): it is what gets
    saved to last-command.wav, forensic value intact. stt_i16 is the
    recorded-frames-only clip starting at the pre-roll boundary and is
    what the STT engines transcribe; None means transcribe the full
    clip."""
    wavfile.write(TOOLS + "/last-command.wav", 16000, audio_i16)
    if stt_i16 is None:
        stt_i16 = audio_i16
    audio = stt_i16.astype(np.float32) / 32768.0
    dur = len(audio) / 16000.0
    text = ""
    touch_progress()  # STT below is the longest silent stretch (<= ~45 s)
    engine = "fw-long" if dur > 8.0 else "fw"
    lang = _stt_lang()
    if lang == "en":
        log("STT_LANG en (switch on)")
    if qwen is not None and dur <= 8.0:
        wavfile.write(TOOLS + "/last-command-mono.wav", 16000, stt_i16)
        try:
            text = qwen.transcribe(TOOLS + "/last-command-mono.wav", lang).strip()
            engine = "qwen"
        except Exception as e:
            log("QWEN_FAIL %r" % e)
            text = ""
    if not text:
        segs, info = stt.transcribe(audio, language=lang, beam_size=1, vad_filter=True, without_timestamps=True, condition_on_previous_text=False)
        text = "".join(x.text for x in segs).strip()
    touch_progress()
    log("STT[%s %.1fs]: %r" % (engine, dur, text))
    # The pre-roll (see preroll_snapshot) usually contains the wake
    # phrase itself; never let it leak into the command text.
    text = strip_wake_prefix(text)
    if not text:
        return (False, False)
    # Anti-hallucination filter: STT repetition loops ("yo, yo, yo..."
    # x60 from weak ambient audio, 2026-10-17) get dropped before they
    # can reach the chat or arm a follow-up window. Natural speech
    # never concentrates one word/bigram like this over 12+ words.
    words = [w.strip(".,;:!?\"'").lower() for w in text.split()]
    words = [w for w in words if w]
    if len(words) >= 12:
        top_word, top_n = Counter(words).most_common(1)[0]
        bigrams = list(zip(words, words[1:]))
        top_bi_n = Counter(bigrams).most_common(1)[0][1] if bigrams else 0
        if top_n / len(words) >= 0.5 or top_bi_n * 2 / len(words) >= 0.6:
            log("HALLUCINATION_DROPPED top=%r %.0f%% bi=%.0f%%" % (top_word, 100 * top_n / len(words), 100 * top_bi_n * 2 / len(words)))
            return (False, False)
    # Wake evidence gate (batch C4): wake turns only — follow-ups
    # have their own acoustic gate. Weak speech evidence + a
    # short fragmentary transcript = an ambient phantom: drop it
    # here (returning (False, False) makes run_turn run this
    # turn's own wall cleanup) and send NOTHING.
    if base in ("studio-mic", "studio-mic-low") and len(words) <= 4:
        weak, (vratio, wfrac, wmed, wp90) = wake_gate_weak(stt_i16)
        if weak:
            tag = "WAKE_TURN_DROPPED" if WAKE_GATE_ACTIVE else "WAKE_GATE_WOULD_DROP"
            log("%s vad=%s frac=%.2f median=%.0f p90=%.0f words=%d text=%r"
                % (tag, ("%.2f" % vratio) if vratio is not None else "n/a",
                   wfrac, wmed, wp90, len(words), text))
            if WAKE_GATE_ACTIVE:
                return (False, False)
    # Echo guard: a follow-up that is really MuseZ's own last spoken
    # reply coming back through the room is dropped before it can
    # reach the chat (no send, no filler, no chained window).
    if base == "studio-mic-followup" and is_echo_of_last_spoken(text):
        log("FOLLOWUP_DROPPED_ECHO %r" % text)
        return (False, False)
    # Mute can land while STT runs (seconds): re-check immediately
    # before the send - a muted turn must never reach the chat. The
    # wall has nothing of this turn's to restore yet (THINKING is
    # only painted after a successful send), and returning
    # (False, False) makes run_turn restore home as usual
    # (Astra review 2026-10-18, batch 1 fix 2).
    if os.path.exists(TOOLS + "/listener-muted.flag"):
        log("TURN_ABORTED_MUTED before send")
        return (False, False)
    # Every send failure funnels to the same cleanup: returning
    # (False, False) makes run_turn restore the wall home. A timeout
    # used to raise TimeoutExpired straight out of run_turn, killing
    # the process and skipping that cleanup entirely. Never retry a
    # failed send: a timeout does not prove non-delivery and there
    # is no dedup key (Astra review 2026-10-18, batch 1 fix 5).
    touch_progress()  # the send can block up to its 60 s timeout
    prefix = "[%s t=%d] " % (base, _active_turn[0])
    # Bridge destination (2026-10-31): with the API switch in
    # bridge mode the turn is queued for the bridge daemon instead
    # of being sent to the chat. Registration matches the classic
    # send exactly (watchdog + THINKING). Any queue failure falls
    # through to the classic send below - never lose a turn to it.
    _filled = False
    if _brain_bridge():
        # Early ack for bridge turns (2026-11-01 night): the bridge's
        # own ack is a TTS dispatch — respond2 renders and ships it,
        # ~12 s before it can sound — so a bridge turn waited in
        # silence for the same ack the classic path gives at once.
        # The pre-recorded filler is the only mechanism that sounds
        # immediately, so a bridge turn gets it too, in the turn's
        # STT language, BEFORE queueing (it covers the daemon's
        # pickup exactly like it covers the classic send). It runs
        # synchronously so the queue entry can carry the truth: when
        # it played, the entry says "filler" and the bridge
        # suppresses its TTS ack; when it was skipped (gate busy or
        # OFF), the bridge's ack remains the backstop. Exactly one
        # acknowledgement per turn, either way.
        _filled = play_filler(lang=_stt_lang())
        if _queue_bridge_turn(base, text, filler=_filled):
            log("BRIDGE_QUEUED turn=%d" % _active_turn[0])
            _register_sent(base, text)
            return (True, base == "studio-mic")
        log("BRIDGE_QUEUE_FAIL turn=%d - falling back to classic send" % _active_turn[0])
    # Batch E (2026-10-23): the acknowledgement filler fires NOW, in
    # a parallel thread, while the send runs — Javier's choreography:
    # the first phrase lands as soon as he finishes speaking (+ STT),
    # not after the SDK round trip. The mute check above already
    # passed, so the turn is real. If the send itself then fails
    # (rare, logged), the ack has promised an answer that never
    # registers with the watchdog — accepted trade, batch E notes.
    # 2026-11-01 morning: the filler started here, on the classic
    # branch only, after bridge turns heard two acks back to back
    # (filler, then the bridge ack). The night fix above keeps the
    # one-ack rule by coordination instead of suppression: a bridge
    # turn that already filled skips this thread even when its queue
    # write failed and it falls back to the classic send.
    if not _filled:
        # 2026-11-02: the classic ack takes the turn's STT language
        # too - with the switch in English, a Spanish filler was the
        # wrong sign of life (the bridge branch already passed it).
        threading.Thread(target=play_filler, kwargs={"lang": _stt_lang()},
                         daemon=True).start()
    try:
        r = subprocess.run(["sudo", "-n", "musegadget", "send-user-msg", prefix + text], capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        log("SEND_FAIL TimeoutExpired")
        return (False, False)
    except Exception as e:
        log("SEND_FAIL %s %r" % (type(e).__name__, e))
        return (False, False)
    if r.returncode != 0:
        log("SEND_FAIL rc=%d err=%s" % (r.returncode, (r.stderr or "")[:120]))
        return (False, False)
    log("SENT rc=%d err=%s" % (r.returncode, (r.stderr or "")[:120]))
    # Batch D/E registration, shared with the bridge destination
    # (see _register_sent above).
    _register_sent(base, text)
    return (True, base == "studio-mic")

def followup_has_voice(audio_i16):
    """Acoustic evidence gate for follow-up captures (2026-10-17):
    two phantom follow-ups today ("El." x2) came from near-silence
    clips with a FLAT RMS envelope (median ~233, one 0.25 s bump to
    526, no voice) that the STT hallucinated words from — and a
    phantom even played a filler and chained another window.
    Rule: drop the turn silently BEFORE STT (no send, no filler, no
    chained window) when fewer than 15% of 0.25 s windows exceed
    2x the clip's median RMS. The measured phantom scored 1/20 (5%).
    Dense-speech rescue: in a clip that is MOSTLY voice the median
    itself is a speech level, so the 2x-median fraction collapses
    even though the envelope is unmistakably voice — measured room
    tone here medians ~230-500 while speech medians are ~1500+, so
    a clip with median >= 600 AND p90 >= 1200 counts as sustained
    voice and passes. Javier's real follow-ups (multi-second voice
    envelopes, with or without pauses) pass on one branch or the
    other; the phantom passes neither."""
    win = 4000  # 0.25 s at 16 kHz
    n = len(audio_i16) // win
    if n == 0:
        log("FOLLOWUP_DROPPED_QUIET windows=0 above=0 frac=0.0% median=0 p90=0")
        return False
    a = audio_i16[:n * win].astype(np.float64).reshape(n, win)
    rms = np.sqrt(np.mean(a ** 2, axis=1))
    med = float(np.median(rms))
    p90 = float(np.percentile(rms, 90))
    above = int(np.sum(rms > 2.0 * max(med, 1.0)))
    frac = above / float(n)
    if frac >= 0.15:
        return True
    if med >= 600.0 and p90 >= 1200.0:
        return True
    log("FOLLOWUP_DROPPED_QUIET windows=%d above=%d frac=%.1f%% median=%.0f p90=%.0f" % (n, above, 100.0 * frac, med, p90))
    return False

# ------------------------------------------------------------------
# Wake pre-send evidence gate (batch C4, 2026-10-18 — Javier
# approved after the 05:19 'El.' and 11:20 'Desde todo el
# liquidación' phantom sends: "el problema acá es que reconoce
# antes hey muse??"). The wake word fires on a sound-alike; this
# gate asks whether the CAPTURE actually contains speech before
# anything is sent. Evidence = Silero VAD speech ratio over the
# STT clip + the followup_has_voice RMS profile. A turn is dropped
# only when the evidence is WEAK *and* the transcript is a short
# fragment (<= 4 words): a real short command ("¿va a llover
# hoy?") carries dense speech evidence and passes — the gate
# keys on audio evidence density, never on text length alone.
#
# Offline calibration (~/voice-venv, 2026-10-18, 12 clips):
#   garbage 11:20 (last-command-mono): vad=0.03 -> DROP
#   phantom species 05:19 (clip overwritten; profile from the
#     followup_has_voice measurements): frac=0.05 -> DROP branch
#   real commands (case0849, q0936, long0958, voice2, forensics
#     x2, dsp-ab x2, wake samples): vad 0.19-0.84 / frac
#     0.17-0.46 -> ALL PASS. Two zero-transcript clips (mic-test,
#     dsp t0849 replay) never reach the gate in production (no
#     text -> process_audio returns before it).
WAKE_GATE_ACTIVE = True

def speech_evidence(clip_i16):
    """(vad_ratio|None, voice_frac, median_rms, p90_rms) of a clip."""
    a = clip_i16.astype(np.float64)
    win = 4000  # 0.25 s
    n = max(1, len(a) // win)
    rms = np.array([float(np.sqrt(np.mean(a[i * win:(i + 1) * win] ** 2)))
                    for i in range(n)])
    med = float(np.median(rms))
    p90 = float(np.percentile(rms, 90))
    frac = float(np.mean(rms > 2.0 * max(med, 1.0)))
    vratio = None
    if vad is not None:
        try:
            vad.reset()
            cnt = 0
            sp = 0
            buf = clip_i16.astype(np.int16)
            for i in range(0, len(buf) - 511, 512):
                if float(vad(buf[i:i + 512].tobytes())) >= 0.5:
                    sp += 1
                cnt += 1
            vratio = (sp / cnt) if cnt else 0.0
        except Exception as e:
            log("WAKE_GATE_VAD_FAIL %r" % e)
            vratio = None
    return vratio, frac, med, p90

def wake_gate_weak(clip_i16):
    """(is_weak, metrics) — see the calibration note above."""
    vratio, frac, med, p90 = speech_evidence(clip_i16)
    if med >= 600.0 and p90 >= 1200.0:
        return False, (vratio, frac, med, p90)  # dense-speech rescue
    weak = (vratio is not None and vratio < 0.10) or frac < 0.10
    return weak, (vratio, frac, med, p90)

def await_followup():
    """Conversation mode: after a normal turn, wait for MuseZ's spoken
    reply (speaking.flag on -> off, up to 25 s; the window opens anyway
    on timeout), then capture one follow-up with no wake word.
    Chaining rule (2026-10-17 polish): a follow-up captured in a
    window chains a NEW watch ONLY IF the preceding exchange had a
    real spoken reply (the flag cycled during this watch). When the
    watch timed out with no reply, the window still opens once, but a
    turn captured there does NOT chain — the mode closes after
    handling it. Before that rule, ANY sent turn re-armed the mode
    (a cough, room noise, MuseZ's own echo, Javier dictating to his
    phone) and the mic appeared to 'activate itself'. Silence and
    gate/echo drops never chain."""
    log("FOLLOWUP_WATCH")
    t0 = time.time()
    saw = False
    replied = False
    while time.time() - t0 < 25:
        if os.path.exists(TOOLS + "/listener-muted.flag"):
            log("FOLLOWUP_ABORT muted")
            return
        c = read_chunk()
        if c is None:
            return
        if os.path.exists(TOOLS + "/speaking.flag"):
            saw = True
        elif saw:
            replied = True
            break
        else:
            preroll_push(c)
    had_reply = replied or saw
    if replied:
        # Reverb guard: let the room tail of MuseZ's own voice die
        # (~1.2 s, drained so the arecord pipe never backs up) before
        # the window starts recording Javier.
        t_guard = time.time()
        while time.time() - t_guard < 1.2:
            if read_chunk() is None:
                return
    log("FOLLOWUP_WINDOW")
    run_turn("followup", None, chain=had_reply)

# Live-turn token (batch A, night policy enforced at the output):
# minted the moment a capture yields real speech. During OFF hours
# the playback layer refuses any session that cannot show a fresh
# token (< 120 s old), so a deliberate "Hey Muse" at 3 AM is still
# answered while stale, delayed or agent-initiated audio (no token)
# stays silent. The token is the layer's file; mint via playgate so
# path and format have a single implementation.
def mint_live_turn_token():
    try:
        playgate.mint_token()
        log("LIVE_TOKEN_MINTED")
    except Exception as e:
        log("TOKEN_FAIL %r" % e)

def run_gate(score):
    """Call gate (2026-11-06): a wake in gate mode opens a live
    voice call instead of a bridge turn. The command is captured
    exactly like a wake turn; the clip (when speech was heard) is
    handed to the call as its first injected input, so speaking
    the request right after the wake word still works. This
    process then exits: the call wrapper (a transient unit of its
    own, so stopping this unit cannot kill it) stops the listener
    unit before systemd's restart backoff fires, owns the call,
    and restarts the listener when the call ends. Returns
    GATE_EXIT on handover, or a normal cooldown if the gate path
    aborts (the listener then stays a listener)."""
    _active_turn[0] = mint_turn()
    touch_progress()
    led_flash_start()
    wall_turn(PH_LISTENING, "avatar=listening", "wave=true")
    pre = preroll_snapshot()
    try:
        rec_i16 = capture_command(7)
    except (TurnMuted, CaptureAborted) as e:
        log("GATE_ABORTED %s" % getattr(e, "tag", repr(e)))
        levels_idle()
        led_flash_stop()
        led_off()
        wall_home()
        return 1.0
    touch_progress()
    wav24 = ""
    if rec_i16 is not None:
        audio_i16 = rec_i16
        if pre is not None:
            audio_i16 = np.concatenate([pre, rec_i16])
        try:
            wavfile.write(TOOLS + "/gate-command.wav", 16000, audio_i16)
            # 24 kHz copy for the call runner's injector (3:2
            # linear upsample, same method as the ALSA backend).
            # Inject EXACTLY what was saved — preroll included
            # (Astra review 2026-11-06: the 24k copy previously
            # dropped the preroll the 16k forensic copy kept).
            n = len(audio_i16)
            if n > 1:
                x_new = np.arange(0, n - 1, 2.0 / 3.0)
                up = np.interp(x_new, np.arange(n),
                               audio_i16.astype(np.float64))
                wavfile.write(GATE_WAV24, 24000, up.astype(np.int16))
                wav24 = GATE_WAV24
                log("GATE_WAV samples16=%d samples24=%d" % (n, len(up)))
        except Exception as e:
            log("GATE_WAV_FAIL %r" % e)
            wav24 = ""
    unit = "musez-callgate-%d" % int(time.time())
    try:
        r = subprocess.run(
            ["systemd-run", "--user", "--unit=" + unit, "--collect",
             CALLWRAP, wav24],
            capture_output=True, timeout=15)
    except Exception as e:
        log("GATE_HANDOVER_FAIL %r" % e)
        wall_home()
        return 1.0
    if r.returncode != 0:
        log("GATE_HANDOVER_FAIL rc=%d %s"
            % (r.returncode, (r.stderr or b"")[:200]))
        wall_home()
        return 1.0
    log("GATE_HANDOVER unit=%s wav=%s" % (unit, wav24 or "none"))
    levels_idle()
    led_flash_stop()
    led_off()
    return GATE_EXIT


def run_turn(kind, score, chain=True):
    """One full turn. Returns the cooldown (seconds) for the main loop.
    chain=False (a follow-up captured after a reply-less watch) means:
    handle the turn, then close conversation mode instead of arming
    another follow-up watch."""
    # Mint this turn's wall-state identity — reserved under the
    # state lock (batch B, see mint_turn): strictly greater than
    # any turn the wall has seen, collision-free across processes.
    _active_turn[0] = mint_turn()
    touch_progress()
    if kind == "wake":
        led_flash_start()
    else:
        led_flash2_start()
    # While Javier speaks — the first question (wake) or a
    # follow-up — the avatar listens and the wall animates the
    # Muse squiggle with his voice (Javier, 2026-10-17: first
    # built for follow-ups, where the avatar used to stay frozen
    # in the previous state for the whole turn; extended to the
    # wake path the same evening at his request). Merge only:
    # the home scene and its title stay untouched.
    wall_turn(PH_LISTENING, "avatar=listening", "wave=true")
    pre = preroll_snapshot()
    try:
        rec_i16 = capture_command(7 if kind == "wake" else 8)
    except TurnMuted:
        # Mute landed mid-capture: abort the turn - nothing was
        # transcribed or sent. capture_command's own tail (levels,
        # LED) was skipped by the raise, so redo it here, then undo
        # this turn's listening paint via wall_home().
        log("TURN_ABORTED_MUTED during capture (%s)" % kind)
        levels_idle()
        led_flash_stop()
        led_off()
        wall_home()
        return 1.0
    except CaptureAborted as e:
        # Capture hygiene (batch A): EOF after speech, a feed
        # discontinuity, or a playback session starting mid-capture.
        # Same contract as a mute abort: nothing is transcribed,
        # nothing is sent, the wall goes home.
        log("%s during capture (%s)" % (e.tag, kind))
        levels_idle()
        led_flash_stop()
        led_off()
        wall_home()
        return 1.0
    touch_progress()
    audio_i16 = rec_i16
    if rec_i16 is not None and pre is not None:
        # Full clip for the saved wav; the STT path keeps rec_i16
        # alone (starts exactly at the pre-roll boundary).
        audio_i16 = np.concatenate([pre, rec_i16])
    # Batch E (2026-10-23): the processing phase (STT + send) paints
    # THINKING from the moment the capture ends — Javier's in-person
    # verdict overrode batch C here: working-then-thinking read
    # backwards to him; he expects thinking + the ack first, then
    # working while the agent works (the post-send paint below).
    # Merge only, like the listening paint: a sticky result on the
    # wall survives until the new turn is actually sent.
    wall_turn(PH_PROCESSING, "avatar=thinking", "wave=false")
    if audio_i16 is None:
        log("NO_SPEECH abort (%s)" % kind)
        wall_home()
        return 1.0
    # Real speech was captured: this is a live turn. Mint the token
    # the playback layer demands during OFF hours (see above).
    mint_live_turn_token()
    if kind == "followup" and not followup_has_voice(audio_i16):
        # Phantom-quiet follow-up: dropped like a silence abort —
        # no STT, no send, no filler, no chained follow-up window.
        wall_home()
        return 1.0
    if kind == "followup":
        base = "studio-mic-followup"
    else:
        base = "studio-mic" if score >= 0.68 else "studio-mic-low"
    sent_ok, is_normal = process_audio(audio_i16, base, stt_i16=rec_i16)
    if not sent_ok:
        wall_home()
        return 1.0
    if chain and FOLLOWUP_ENABLED:
        await_followup()
    return 5.0

reconcile_speaking_flag()
last_reconcile = time.monotonic()
cooldown_until = 0.0
was_gated = False
while True:
    mono = read_chunk()
    if mono is None:
        # EOF is the arecord-died path; a reader_error means the
        # reader thread itself died (its READER_DEAD line is in the
        # log). Either way this process is done: exit and let
        # systemd start a fresh listener instead of waiting forever
        # on a condition nobody will ever notify again.
        if feed.reader_error is not None:
            log("READER_ERROR_EXIT %s: %r" % (type(feed.reader_error).__name__, feed.reader_error))
        else:
            log("AUDIO_EOF")
        break
    touch_progress()
    if time.monotonic() - last_reconcile >= 30.0:
        last_reconcile = time.monotonic()
        reconcile_speaking_flag()
    if gated() or time.monotonic() < cooldown_until:
        # Drained by the reader thread and discarded here: the chunk
        # is consumed (so nothing piles up) but never predicted on,
        # never pushed to the pre-roll.
        was_gated = True
        continue
    if was_gated:
        # Boundary (Astra review §3): whatever is still queued from
        # before the gated/busy stretch is dropped wholesale, the
        # detector restarts clean and the pre-roll cannot concatenate
        # audio across the gap.
        was_gated = False
        feed.purge_now()
        _preroll.clear()
        ww.reset()
        continue
    preroll_push(mono)
    s = float(ww.predict(mono).get("hey_muse", 0.0))
    if s <= 0.5:
        continue
    log("WAKE score=%.3f" % s)
    ww.reset()
    # The cooldown clock starts when the turn ENDS, not before it
    # runs: the old one-liner read time.time() first and only then
    # executed the whole turn, so a 12 s turn returning a 5 s
    # cooldown was already 7 s expired on return. Every run_turn
    # return flows through here (5 s after a sent turn, 1 s after a
    # no-speech or failed one), so this single site covers them all.
    # Monotonic clock throughout (Astra review 2026-10-18, fix 1).
    if callgate_enabled():
        delay = run_gate(s)
        if delay == GATE_EXIT:
            log("GATE_EXIT handover done; listener exiting")
            break
    else:
        delay = run_turn("wake", s)
    # Drop the backlog that accumulated in the feed while this turn
    # processed (STT / send / filler consume no chunks): without the
    # purge the detector would chew through seconds of stale audio —
    # possibly the tail of MuseZ's own reply — as if it were live.
    feed.purge_now()
    _preroll.clear()
    was_gated = True
    cooldown_until = time.monotonic() + delay
