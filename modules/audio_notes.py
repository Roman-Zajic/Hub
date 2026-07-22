"""
Audio Notes module — live speech-to-text from the microphone and/or system
(loopback) audio, using Whisper. Transcribed text streams into a persistent,
freely-editable "sandbox" textarea whose content survives an app restart.
Also supports dropping in an existing audio/video file for a one-off
transcription, and recording mic + system audio together, mixed into a
single WAV file.

Drop this file in modules/ to enable the Audio Notes tab in the sidebar;
remove it (or rename it with a leading underscore) to take it out of the
app entirely — no changes to app.py or layout.html are needed either way.

Third-party requirements (installed on demand — see the lazy imports below):
    pip install faster-whisper pyaudiowpatch numpy

pyaudiowpatch is Windows-only (it wraps WASAPI loopback so "system audio"
capture is possible at all); this module is written for the same Windows
target as time.py.

IMPORTANT — app.py's dev server: the file-upload transcription route can
run for a while on a long video. Flask's built-in server handles requests
one at a time unless started with threaded=True, which would otherwise
freeze the rest of the app (including this module's own live polling) for
the duration of that request. Add threaded=True to the app.run(...) call
in app.py to avoid that.
"""
import ctypes
import os
import queue
import threading
import time as time_module
import traceback
import uuid
import wave
from datetime import datetime

from flask import Blueprint, render_template, request, jsonify

# ── Module metadata (read by app.py's auto-discovery) ───────────────
NAV_LABEL = 'Audio Notes'
NAV_PATH = '/audio_notes'
ORDER = 50

bp = Blueprint('audio_notes', __name__)

# ── Config ───────────────────────────────────────────────────────────
# Whisper only ever runs on audio the VAD below classifies as voice — silence
# and background hum never reach the model, which is both faster (no wasted
# inference calls) and produces a cleaner transcript (no hallucinated text
# from dead air). Frames are analyzed in small windows; a run of voiced
# frames opens an "utterance", which is queued for transcription once a run
# of silence (the hangover) closes it again.
FRAME_SECONDS = 0.03              # analysis window for the VAD (~30ms)
VAD_ENTER_FRAMES = 2              # consecutive voiced frames to open an utterance
VAD_HANGOVER_SECONDS = 0.6        # keep buffering this long after voice stops, to catch trailing words
VAD_MIN_UTTERANCE_SECONDS = 0.3   # discard anything shorter — almost certainly a blip, not speech
VAD_MAX_UTTERANCE_SECONDS = 20    # force a flush even mid-speech, so long monologues don't build up latency
VAD_THRESHOLD_MULTIPLIER = 3.0    # how far above the tracked noise floor counts as "voice"
VAD_MIN_ABS_RMS = 0.006           # absolute floor, so a very quiet room's noise floor can't drift to ~0

# How often (in seconds of audio) a running capture stream re-checks whether
# the OS's default mic / default playback (loopback) device has changed —
# e.g. because the user switched to a different headset. A PyAudio stream is
# bound to one specific device index at open time and does NOT automatically
# follow a Windows default-device switch, so without this re-check, capture
# would keep silently listening to whichever device was "default" at the
# moment Mic/System was toggled on, even after you switch headsets.
DEVICE_CHECK_SECONDS = 2.0

WHISPER_DEVICE = 'cpu'
WHISPER_COMPUTE_TYPE = 'int8'
# Explicit thread count instead of letting ctranslate2 auto-detect: on
# weaker/older CPUs "auto" can pick more threads than the machine can
# usefully run in parallel, and the extra context-switching costs more than
# it gains. Leaving one core free for the audio capture thread(s) is a
# better default on modest hardware. Override with the AUDIO_NOTES_CPU_THREADS
# env var if you want to tune it for a specific machine.
WHISPER_CPU_THREADS = int(os.environ.get(
    'AUDIO_NOTES_CPU_THREADS', max(1, (os.cpu_count() or 4) - 1)
))
# Live mic/system audio is already segmented by our own VAD in _capture_loop
# before it ever reaches Whisper (see _process_and_emit), so Whisper's own
# internal VAD pass (vad_filter=True) is redundant work on that path — pure
# overhead on audio that's already been filtered to voice-only. Uploaded
# files (_transcribe_file) have no such pre-filtering, so they keep it on.
LIVE_VAD_FILTER = False
FILE_TRANSCRIBE_BEAM_SIZE = 5     # uploaded files are offline/batch, so it's worth spending more compute for quality

# Models selectable from the dropdown in the UI. distil-* and *.en models are
# English-only. Roughly fastest/roughest -> slowest/most-accurate on CPU.
#
# No official (or community) "small" distilled Polish/multilingual Whisper
# model exists as of this writing — distil-whisper is English-only by
# design; the only Polish "distil" checkpoint found anywhere is a
# community distillation of large-v3 (much heavier than "small", a
# different tier entirely), so it isn't listed here. Plain 'small' below
# is the multilingual option to reach for instead when a call needs
# non-English support — see the "model selected at queue time" fix in
# _capture_loop/_process_and_emit for why switching to it mid-call is now
# safe for anything already queued under the previous model.
AVAILABLE_MODELS = [
    ('tiny.en', 'Tiny (English) — fastest, roughest'),
    ('base', 'Base — fast Whisper'),
    ('small', 'Small — balanced, multilingual'),
    ('distil-small.en', 'Distil Small (English) — default, fast + better than base'),
    ('distil-medium.en', 'Distil Medium (English) — balanced'),
    ('medium', 'Medium — slower, multilingual'),
]
DEFAULT_MODEL = 'distil-small.en'

# ── File paths ───────────────────────────────────────────────────────
DATA_FOLDER = os.path.join(os.getcwd(), 'Audio Notes Data')
DAILY_NOTES_FOLDER = os.path.join(DATA_FOLDER, 'Daily Notes')
UPLOADS_TMP_FOLDER = os.path.join(DATA_FOLDER, 'tmp_uploads')
os.makedirs(DATA_FOLDER, exist_ok=True)
os.makedirs(DAILY_NOTES_FOLDER, exist_ok=True)
os.makedirs(UPLOADS_TMP_FOLDER, exist_ok=True)

CONTENT_FILE = os.path.join(DATA_FOLDER, 'content.txt')
ERROR_LOG = os.path.join(DATA_FOLDER, 'audio_notes_error.log')

# ── Shared state ─────────────────────────────────────────────────────
_lock = threading.Lock()          # guards _content / _pending_chunks
_content = ''                     # authoritative persisted sandbox text
_pending_chunks = []               # new transcript text waiting for the frontend

_state_lock = threading.Lock()    # guards thread/flag bookkeeping below

# ── Shared PyAudio instance ───────────────────────────────────────────
# pyaudiowpatch/PortAudio does not tolerate multiple independent
# PyAudio() instances existing at once in the same process: terminating
# ANY one of them tears down shared WASAPI state for ALL of them. That is
# what caused "OSError: [Errno -9988] Stream closed" on one stream (e.g.
# mic) whenever another (system audio, or a Record toggle) was stopped
# and its own PyAudio() instance got terminate()'d — which also explains
# mic/system capture silently going down to "only one source working"
# when both were toggled on. Fix: never create more than one PyAudio()
# instance per process — every consumer (live mic, live system, and both
# recording readers) acquires this same shared instance and releases it
# when done; it's only actually terminate()'d once the last consumer
# releases it.
_pa_lock = threading.Lock()
_pa_instance = None
_pa_refcount = 0


def _pa_acquire():
    global _pa_instance, _pa_refcount
    with _pa_lock:
        if _pa_instance is None:
            import pyaudiowpatch as pyaudio
            _pa_instance = pyaudio.PyAudio()
        _pa_refcount += 1
        return _pa_instance


def _pa_release():
    global _pa_instance, _pa_refcount
    with _pa_lock:
        _pa_refcount = max(0, _pa_refcount - 1)
        if _pa_refcount == 0 and _pa_instance is not None:
            try:
                _pa_instance.terminate()
            except Exception:
                _log_error('pyaudio terminate')
            _pa_instance = None

# Live capture: a persistent queue per stream decouples audio reading from
# transcription (see _capture_loop / _merge_worker_loop below) — this is
# the fix for audio being dropped while a chunk is transcribing.
#
# Mic and System Audio each have their own capture pipeline, but two
# different things can independently ask for either one to be running:
#   - _mic_to_textarea / _system_to_textarea — the two toolbar buttons that
#     decide whether that source's transcript streams into the on-screen
#     textarea.
#   - _daily_log_active — the single Daily Notes button, which always wants
# A source's capture thread runs whenever EITHER consumer wants it — see
# _sync_mic_capture() / _sync_system_capture(), called after every toggle.
# Mic and System-to-textarea both default to OFF. Daily Log defaults to ON,
# which — per _sync_mic_capture/_sync_system_capture — pulls BOTH mic and
# system-audio capture on with it even though their own textarea buttons
# stay off.
#
# System Audio must be started with a manual click / a real Flask request
# thread — NEVER auto-started directly from on_load(). This is not
# optional: auto-starting WASAPI loopback from a background thread at app
# boot has twice caused a hard process crash (an unrecoverable native
# access violation, exit code -1073741819 / 0xC0000005 — no Python
# traceback, since it's not a catchable exception) — once via a dedicated
# daily system-audio thread, and again when System was simply auto-started
# the same way Mic used to be. A manual click, or any call made from
# inside a Flask route handler (a real request thread), has been reliably
# fine — same as Record already works.
#
# Since Daily Log now defaults to ON, satisfying that default still means
# System Audio (and Mic) capture need to start automatically at some
# point — just never inside on_load() itself. _ensure_boot_capture_started()
# below does this exactly once, the first time any audio_notes route is hit
# (i.e. the moment the page is opened in a browser, which is a real request
# thread), rather than at process startup.
_mic_to_textarea = False
_mic_capture_thread = None
_mic_stop_event = None
_mic_queue = queue.Queue()

_system_to_textarea = False
_system_capture_thread = None
_system_stop_event = None
_system_queue = queue.Queue()

# Transcription itself is done by a SINGLE global worker thread (see
# _merge_worker_loop below, started once from on_load()), not one worker
# thread per source. Mic and System each still get their own independent
# capture (VAD-segmenting) thread and queue — only the "consume +
# transcribe" side is merged, so utterances from both sources get
# transcribed in true chronological order (by when they started being
# spoken) instead of in whichever order two concurrent worker threads
# happen to finish.
_mic_processing = False       # True while a mic utterance is actively being transcribed
_system_processing = False    # True while a system-audio utterance is actively being transcribed

# Daily Log (Audio Notes Data/Daily Notes/YYYY-MM-DD.txt). One button, on
# when active it ensures BOTH mic and system-audio capture are running (see
# _sync_mic_capture / _sync_system_capture) and writes every utterance from
# either source to the day's file — independent of whichever sources the
# textarea buttons above have chosen to stream on-screen. See
# _emit_transcript, which is the single place both destinations are fed
# from, using the exact same tagged "[HH:MM:SS] [Mic/Sys] text [HH:MM:SS]"
# format in both places.
_daily_log_active = True

# Count of utterances currently sitting in _mic_queue/_system_queue that
# are actually destined for the Daily Log (i.e. utterance_wants_daily_log
# was True at capture time — see _capture_loop) and haven't been
# transcribed yet. Daily Log runs silently in the background — often with
# the textarea buttons themselves toggled off — so qsize() on the raw
# queues isn't enough to tell whether anything is piling up specifically
# for the log file; this is what the "queued" badge next to the Daily Log
# button (poll_route's 'daily_log_queue_depth') is driven by. Incremented
# in _capture_loop right when such an utterance is queued, decremented in
# _merge_worker_loop right when it's dequeued for transcription.
_daily_log_queue_lock = threading.Lock()
_daily_log_queue_depth = 0


def _daily_log_queue_incr():
    global _daily_log_queue_depth
    with _daily_log_queue_lock:
        _daily_log_queue_depth += 1


def _daily_log_queue_decr():
    global _daily_log_queue_depth
    with _daily_log_queue_lock:
        _daily_log_queue_depth = max(0, _daily_log_queue_depth - 1)

# Guards _ensure_boot_capture_started() (see on_load()/audio_notes_view()/
# poll_route() below) so the deferred boot-time sync described above runs
# exactly once, and only from a request thread.
_boot_capture_synced = False

# Mic -> WAV recording (independent of live transcription capture above)

_recording_active = False
_recording_thread = None
_recording_stop_event = None
_recording_path = None
_recording_started_at = None   # epoch seconds, for the frontend's elapsed timer

_selected_model_name = DEFAULT_MODEL   # name the user has currently chosen via the dropdown
# NOTE: the actual loaded-model cache (_model_cache / _model_lock /
# _model_loading / _model_loading_name) is defined further down, right
# next to _get_model() — see the comment there for why it's a small keyed
# cache rather than a single slot.


def _warm_model():
    """Loads the currently-selected model once, ahead of any real audio.
    Run on a daemon thread from on_load() so app startup itself isn't
    blocked — but the (possibly slow, especially on an old/first-run
    machine) load cost is paid before the user ever starts talking, instead
    of stalling the very first utterance. Unlike the mic/system audio
    threads, this touches no WASAPI/COM state, so it's safe to start here
    rather than needing a request-thread click."""
    try:
        _get_model(_selected_model_name)
    except Exception:
        _log_error('model warm-up')


def on_load():
    """Called once by app.py when this module is registered at startup."""
    global _content
    _content = _load_content()
    _cleanup_stale_content_tmp()
    _cleanup_stale_uploads()
    threading.Thread(target=_warm_model, daemon=True).start()
    # The single merge-transcription worker (see _merge_worker_loop) runs
    # for the whole process lifetime, independent of Mic/System being
    # toggled on/off. It touches no WASAPI/COM state — it only reads from
    # the two queues that _capture_loop threads feed and calls Whisper —
    # so, unlike starting capture itself, it's safe to launch directly
    # from on_load()'s background thread.
    threading.Thread(target=_merge_worker_loop, daemon=True).start()
    # Deliberately does NOT call _sync_mic_capture()/_sync_system_capture()
    # here. Daily Log defaults to ON (see _daily_log_active above), which
    # means satisfying that default requires starting System Audio capture
    # — and auto-starting WASAPI loopback from a background thread at app
    # boot is a confirmed, repeatable hard-crash trigger (see the long
    # comment on _mic_to_textarea above). Actually starting mic/system
    # capture to match the defaults is instead deferred to
    # _ensure_boot_capture_started(), called from audio_notes_view() and
    # poll_route() below — both of which only ever run inside a real Flask
    # request thread, never at process startup.


def _ensure_boot_capture_started():
    """Brings capture threads in line with the default toggle states
    (Daily Log ON, which needs both Mic and System running) exactly once,
    the first time any audio_notes route is hit. This exists purely so
    that start happens from a genuine Flask request thread rather than
    from on_load()'s background thread — see the crash-safety note on
    _mic_to_textarea above for why that distinction matters. Safe to call
    on every request after the first: it no-ops immediately once the
    one-time sync has run."""
    global _boot_capture_synced
    if _boot_capture_synced:
        return
    with _state_lock:
        if _boot_capture_synced:
            return
        _boot_capture_synced = True
    try:
        _sync_mic_capture()
        _sync_system_capture()
    except Exception:
        _log_error('boot capture sync')


# ── Error logging (never let a logging failure crash the caller) ─────

def _log_error(context):
    try:
        with open(ERROR_LOG, 'a', encoding='utf-8') as f:
            f.write(f'--- {time_module.strftime("%Y-%m-%d %H:%M:%S")} [{context}] ---\n')
            f.write(traceback.format_exc())
            f.write('\n')
    except Exception:
        pass


# ── COM initialization for WASAPI threads ─────────────────────────────
# pyaudiowpatch's WASAPI backend (used for both mic and, especially, system-
# audio loopback) relies on COM internally. COM is initialized automatically
# on a process's main thread in many contexts, but NOT on a plain
# threading.Thread — calling into WASAPI from such a thread without first
# initializing COM there can crash the ENTIRE PYTHON PROCESS outright (an
# unrecoverable native access violation — no traceback, no exception to
# catch, just the process disappearing) rather than raising anything
# Python-level. Every thread that opens a pyaudiowpatch stream must call
# _com_thread_init() first and _com_thread_uninit() when it's done.
_COINIT_MULTITHREADED = 0x0


def _com_thread_init():
    try:
        ctypes.windll.ole32.CoInitializeEx(None, _COINIT_MULTITHREADED)
    except Exception:
        _log_error('COM init')


def _com_thread_uninit():
    try:
        ctypes.windll.ole32.CoUninitialize()
    except Exception:
        pass


def _cleanup_stale_content_tmp():
    tmp = CONTENT_FILE + '.tmp'
    if os.path.exists(tmp):
        try:
            os.remove(tmp)
        except Exception:
            pass


def _cleanup_stale_uploads():
    """tmp_uploads/ is always transient (files are deleted right after
    transcription); anything found here at startup is leftover from a
    crash mid-request, so it's safe to wipe."""
    try:
        for name in os.listdir(UPLOADS_TMP_FOLDER):
            try:
                os.remove(os.path.join(UPLOADS_TMP_FOLDER, name))
            except Exception:
                pass
    except Exception:
        pass


# ── Content persistence (atomic write, same pattern as time.py) ──────

def _load_content():
    if os.path.exists(CONTENT_FILE):
        try:
            with open(CONTENT_FILE, 'r', encoding='utf-8') as f:
                return f.read()
        except Exception:
            _log_error('load_content')
            return ''
    return ''


def _save_content(text):
    tmp = CONTENT_FILE + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            f.write(text)
        os.replace(tmp, CONTENT_FILE)
    except Exception:
        _log_error('save_content')
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except Exception:
                pass


def _format_tagged_line(start_dt, end_dt, text, is_system):
    """[HH:MM:SS] [Mic/Sys] transcribed text [HH:MM:SS] — the single format
    shared by both the Daily Log file and the on-screen textarea, so a line
    looks and reads identically wherever it ends up."""
    tag = 'Sys' if is_system else 'Mic'
    return f'[{start_dt.strftime("%H:%M:%S")}] [{tag}] {text} [{end_dt.strftime("%H:%M:%S")}]'


def _emit_transcript(text, start_dt, end_dt, is_system, wants_textarea, wants_daily_log):
    """Routes one finished utterance to whichever destination(s) wanted
    that source AT THE MOMENT IT WAS CAPTURED (wants_textarea /
    wants_daily_log, stamped by _capture_loop when the utterance opened —
    see utterance_wants_textarea / utterance_wants_daily_log there), not
    whichever toggle state happens to be current by the time transcription
    actually finishes. Without this, toggling Mic/System-to-textarea or
    Daily Log off while a backlog is still queued would silently drop
    those already-spoken utterances from a destination they were actually
    recorded for — turning it off is a "stop capturing new stuff for this
    destination from now on", not "un-happen everything already queued".
    A given utterance is transcribed exactly once here regardless of how
    many destinations want it — only the routing differs."""
    global _content
    if not text:
        return

    line = _format_tagged_line(start_dt, end_dt, text, is_system)

    if wants_textarea:
        with _lock:
            sep = '' if (not _content or _content.endswith('\n')) else '\n'
            _content += sep + line + '\n'
            _pending_chunks.append(line + '\n')
            _save_content(_content)

    if wants_daily_log:
        _append_daily_log(start_dt, line)


# ── Whisper model (lazy — (re)loaded on demand, small keyed cache) ────
# Switching the dropdown just updates _selected_model_name; the actual
# (potentially slow, first-download) load happens in _get_model(), the
# next time something needs to transcribe.
#
# This is a small cache keyed by model name, not a single slot, and
# _get_model() takes an explicit `target` rather than reading
# _selected_model_name itself. That's deliberate: each queued utterance is
# stamped (in _capture_loop, at the moment it's queued) with whichever
# model was selected right then, and is transcribed with THAT model in
# _process_and_emit — not whatever the dropdown has since moved on to. In
# a meeting where the language switches (e.g. English -> multilingual for
# a Polish segment -> English again), a backlog of English-tagged
# utterances that hasn't been drained yet stays correctly tagged English
# even after the dropdown is flipped to multilingual and back, instead of
# silently being re-transcribed with whatever model happens to be
# "current" by the time the worker thread gets to them.
#
# Keeping a small cache (rather than reloading from scratch on every
# single switch) means toggling between the same two models repeatedly
# during one call doesn't pay the full load cost each time. Capped at
# _MODEL_CACHE_MAX so memory doesn't grow unbounded if many different
# models get selected over a long session — the least-recently-used
# model is evicted first.
_model_cache = {}          # model name -> loaded WhisperModel instance
_model_cache_order = []    # model names, least-recently-used first
_MODEL_CACHE_MAX = 2
_model_lock = threading.Lock()
_model_loading = False     # True while ANY (re)load is in progress
_model_loading_name = None  # which model name is currently being loaded, if any


def _touch_model_cache(name):
    if name in _model_cache_order:
        _model_cache_order.remove(name)
    _model_cache_order.append(name)


def _get_model(target):
    """Returns the loaded WhisperModel for `target`, loading it (and
    evicting the least-recently-used cached model if the cache is full)
    if it isn't already resident."""
    global _model_loading, _model_loading_name
    with _model_lock:
        if target in _model_cache:
            _touch_model_cache(target)
            return _model_cache[target]
        _model_loading = True
        _model_loading_name = target
        try:
            from faster_whisper import WhisperModel
            model = WhisperModel(
                target,
                device=WHISPER_DEVICE,
                compute_type=WHISPER_COMPUTE_TYPE,
                cpu_threads=WHISPER_CPU_THREADS,
            )
            _model_cache[target] = model
            _touch_model_cache(target)
            while len(_model_cache_order) > _MODEL_CACHE_MAX:
                oldest = _model_cache_order.pop(0)
                _model_cache.pop(oldest, None)
            return model
        finally:
            _model_loading = False
            _model_loading_name = None


def _model_is_english_only(name):
    return 'distil' in name or name.endswith('.en')


# ── Audio helpers (numpy imported lazily so it's optional for the rest
#    of the app if this module's dependencies aren't installed yet) ───

def _resample_linear(samples, orig_sr, target_sr):
    import numpy as np
    if orig_sr == target_sr or len(samples) < 2:
        return samples
    duration = len(samples) / orig_sr
    target_len = max(1, int(round(duration * target_sr)))
    orig_idx = np.linspace(0, len(samples) - 1, num=len(samples))
    target_idx = np.linspace(0, len(samples) - 1, num=target_len)
    return np.interp(target_idx, orig_idx, samples).astype(np.float32)


def _process_and_emit(label, raw_bytes, channels, rate, start_dt, end_dt, is_system, model_name,
                       wants_textarea, wants_daily_log):
    """Transcribes one already-VAD-segmented utterance (live mic/system
    capture). Runs on the single merge-worker thread, never on the
    audio-reading thread — see _capture_loop / _merge_worker_loop."""
    try:
        import numpy as np
        samples = np.frombuffer(raw_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        if channels > 1:
            samples = samples.reshape(-1, channels).mean(axis=1)
        samples = _resample_linear(samples, rate, 16000)

        # model_name is the model that was SELECTED AT THE MOMENT this
        # utterance was queued (stamped by _capture_loop) — not whatever
        # _selected_model_name / the dropdown has since moved on to. This
        # is what makes a backlog transcribe correctly with the language
        # it was actually captured under, even if the dropdown changed
        # (e.g. English -> multilingual -> English) before the worker
        # thread got around to draining it.
        model = _get_model(model_name)
        lang = 'en' if _model_is_english_only(model_name) else None

        # condition_on_previous_text=False is the setting recommended for the
        # distil-* checkpoints, to stop them fixating on earlier chunk text.
        # LIVE_VAD_FILTER is off by default: this audio has already been
        # through the utterance-level VAD in _capture_loop, which is what
        # actually stops silence and ambient-noise false-triggers (the
        # "Okay." / "Yeah." hallucinations) from reaching here at all.
        # Whisper's own internal VAD pass on top of that is redundant work —
        # set LIVE_VAD_FILTER = True above if you ever want that extra,
        # finer-grained pass back (e.g. while tuning the VAD_* constants),
        # at the cost of some speed.
        segments, _ = model.transcribe(
            samples,
            language=lang,
            beam_size=1,
            vad_filter=LIVE_VAD_FILTER,
            condition_on_previous_text=False,
        )
        text = ' '.join(seg.text.strip() for seg in segments).strip()
        if text:
            # wants_textarea / wants_daily_log are likewise stamped at
            # capture time, not read fresh here — see _emit_transcript for
            # why: toggling either off while this utterance was still
            # queued must not make an already-spoken line vanish from a
            # destination it was actually captured for.
            _emit_transcript(text, start_dt, end_dt, is_system, wants_textarea, wants_daily_log)
    except Exception:
        _log_error(f'{label}: transcribe')


def _daily_log_path_for(dt):
    return os.path.join(DAILY_NOTES_FOLDER, dt.strftime('%Y-%m-%d') + '.txt')


def _append_daily_log(start_dt, line):
    """Appends one already-formatted tagged line (see _format_tagged_line)
    to the day's log file — the file for the day the utterance STARTED in,
    so something spanning midnight still lands under the day it began."""
    path = _daily_log_path_for(start_dt)
    try:
        with open(path, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except Exception:
        _log_error('daily log: append')


def _load_wav_as_samples(path):
    """Decodes a .wav file directly with the stdlib wave module (+ numpy),
    entirely bypassing faster-whisper's own decoder — which relies on the
    "av" package (PyAV/ffmpeg) and fails outright if that isn't installed.
    This covers the most common upload case, including files made by this
    module's own Record button, without needing that extra dependency."""
    import numpy as np
    with wave.open(path, 'rb') as wf:
        channels = wf.getnchannels()
        rate = wf.getframerate()
        sampwidth = wf.getsampwidth()
        raw = wf.readframes(wf.getnframes())

    if sampwidth == 2:
        data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    elif sampwidth == 1:
        # WAV's 8-bit format is unsigned, unlike 16/32-bit
        data = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif sampwidth == 4:
        data = np.frombuffer(raw, dtype=np.int32).astype(np.float32) / 2147483648.0
    else:
        raise ValueError(f'Unsupported WAV sample width: {sampwidth} bytes')

    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)

    return _resample_linear(data, rate, 16000)


def _transcribe_file(path):
    """Transcribes an uploaded audio/video file in one shot (not live).
    .wav files are decoded directly (see _load_wav_as_samples), which needs
    no extra dependencies. Anything else (mp4, mp3, m4a, mov, etc.) is
    passed straight to faster-whisper's own decoder (PyAV/ffmpeg under the
    hood), which pulls the audio track out of a video container directly —
    but does require the "av" package to be installed."""
    model_name = _selected_model_name
    model = _get_model(model_name)
    lang = 'en' if _model_is_english_only(model_name) else None

    if os.path.splitext(path)[1].lower() == '.wav':
        audio_input = _load_wav_as_samples(path)
    else:
        audio_input = path

    segments, _ = model.transcribe(
        audio_input,
        language=lang,
        beam_size=FILE_TRANSCRIBE_BEAM_SIZE,
        vad_filter=True,
        condition_on_previous_text=False,
    )
    return '\n'.join(seg.text.strip() for seg in segments if seg.text.strip())


def _set_processing(label, value):
    global _mic_processing, _system_processing
    if label == 'mic':
        _mic_processing = value
    else:
        _system_processing = value


# ── Capture (producer) + worker (consumer) — the fix for dropped audio ─
# _capture_loop's only job is reading the stream and segmenting it by voice
# activity; it NEVER calls Whisper directly, so it can never be blocked by
# a slow transcription. Finished utterances are handed to a queue instead;
# the single _merge_worker_loop thread (shared across both mic and system
# — see its docstring) drains both queues in chronological order and does
# the actual (slow) transcription. If transcription temporarily falls
# behind, utterances queue up rather than audio getting silently dropped by
# the OS-level stream buffer overflowing — a lagging transcript, never
# lost speech.

def _resolve_capture_device(p, pyaudio, is_system):
    """Resolves whichever device Windows *currently* considers the default
    (default input mic, or the loopback twin of the default playback device
    for system audio). Called both when a stream is (re)opened and
    periodically while it's running (see DEVICE_CHECK_SECONDS in
    _capture_loop / _record_reader_loop) so a headset switch made mid-call
    is picked up instead of the stream staying pinned to whatever was
    default when it first opened."""
    if is_system:
        wasapi_info = p.get_host_api_info_by_type(pyaudio.paWASAPI)
        device_info = p.get_device_info_by_index(wasapi_info['defaultOutputDevice'])
        if not device_info.get('isLoopbackDevice'):
            for loopback in p.get_loopback_device_info_generator():
                if device_info['name'] in loopback['name']:
                    device_info = loopback
                    break
    else:
        device_info = p.get_default_input_device_info()
    return device_info


def _capture_loop(label, stop_event, is_system, out_queue, max_utterance_seconds=VAD_MAX_UTTERANCE_SECONDS):
    _com_thread_init()
    try:
        import pyaudiowpatch as pyaudio
        import numpy as np
    except ImportError:
        _log_error(f'{label}: pyaudiowpatch/numpy is not installed')
        _com_thread_uninit()
        return

    p = None
    try:
        p = _pa_acquire()

        # VAD state lives OUTSIDE the reconnect loop below, so switching the
        # active headset mid-utterance doesn't reset the noise floor for no
        # reason — only the underlying PyAudio stream gets torn down and
        # reopened against whatever the new default device is.
        noise_floor = VAD_MIN_ABS_RMS
        voiced_run = 0
        silence_run = 0
        in_speech = False
        utterance_buf = bytearray()
        utterance_frames = 0
        utterance_start_dt = None
        # Stamped with _selected_model_name the moment an utterance opens
        # (see below) and carried through to every out_queue.put() for
        # that utterance, so it's transcribed with the model that was
        # actually selected at capture time — even if the dropdown moves
        # on to something else before a backlogged queue gets drained.
        utterance_model_name = None
        # Same idea, for routing: stamped with whether the textarea button
        # and Daily Log were on AT THE MOMENT this utterance was captured,
        # so toggling either off while a backlog is still queued doesn't
        # retroactively make an already-spoken utterance vanish from a
        # destination it was recorded for (see _emit_transcript).
        utterance_wants_textarea = None
        utterance_wants_daily_log = None
        channels = 1
        rate = 16000

        # ── Outer loop: (re)connect to the current default device ──────
        # Each pass resolves the current default and opens a stream against
        # it; the inner loop reads frames from that stream until either the
        # capture is stopped, or a periodic check (every DEVICE_CHECK_SECONDS
        # of audio) notices the OS default device has changed — e.g. because
        # a second headset was switched to become the active mic/speaker.
        # On a device change, the current stream is closed and we loop back
        # around to reconnect to the new default, instead of silently
        # continuing to listen to the old, no-longer-active device.
        while not stop_event.is_set():
            try:
                device_info = _resolve_capture_device(p, pyaudio, is_system)
            except Exception:
                _log_error(f'{label}: resolve device')
                time_module.sleep(1.0)
                continue

            device_index = device_info['index']
            device_name = device_info['name']
            channels = max(1, int(device_info['maxInputChannels']))
            rate = int(device_info['defaultSampleRate'])
            frames_per_buffer = max(160, int(rate * FRAME_SECONDS))

            try:
                stream = p.open(
                    format=pyaudio.paInt16,
                    channels=channels,
                    rate=rate,
                    input=True,
                    frames_per_buffer=frames_per_buffer,
                    input_device_index=device_index,
                )
            except Exception:
                _log_error(f'{label}: open stream')
                time_module.sleep(1.0)
                continue

            frame_seconds_actual = frames_per_buffer / float(rate)
            hangover_frames = max(1, int(VAD_HANGOVER_SECONDS / frame_seconds_actual))
            max_utterance_frames = max(1, int(max_utterance_seconds / frame_seconds_actual))
            min_utterance_frames = max(1, int(VAD_MIN_UTTERANCE_SECONDS / frame_seconds_actual))
            device_check_every = max(1, int(DEVICE_CHECK_SECONDS / frame_seconds_actual))
            frames_since_check = 0
            device_changed = False
            # Counts consecutive failed stream.read() calls. A genuinely
            # dead/closed stream handle (e.g. "OSError: [Errno -9988]
            # Stream closed") would otherwise retry against the same dead
            # handle forever, silently going deaf on that source. After
            # enough consecutive failures, force a full reconnect via the
            # same path already used for a headset switch.
            consecutive_read_errors = 0

            try:
                while not stop_event.is_set():
                    try:
                        data = stream.read(frames_per_buffer, exception_on_overflow=False)
                        consecutive_read_errors = 0
                    except Exception:
                        _log_error(f'{label}: stream read')
                        consecutive_read_errors += 1
                        if consecutive_read_errors >= 20:
                            device_changed = True  # reuse the reconnect path below
                            break
                        time_module.sleep(0.5)
                        continue

                    frame = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
                    if channels > 1:
                        frame = frame.reshape(-1, channels).mean(axis=1)
                    rms = float(np.sqrt(np.mean(np.square(frame)))) if len(frame) else 0.0

                    threshold = max(VAD_MIN_ABS_RMS, noise_floor * VAD_THRESHOLD_MULTIPLIER)
                    voiced_frame = rms > threshold

                    if voiced_frame:
                        voiced_run += 1
                        silence_run = 0
                    else:
                        silence_run += 1
                        voiced_run = 0
                        # Only chase the noise floor while we're confident we're
                        # NOT in speech, so a long utterance can't drag the floor
                        # up and make the VAD deaf to quieter follow-on speech.
                        if not in_speech:
                            noise_floor = noise_floor * 0.98 + rms * 0.02

                    if not in_speech and voiced_run >= VAD_ENTER_FRAMES:
                        in_speech = True
                        utterance_buf = bytearray()
                        utterance_frames = 0
                        utterance_start_dt = datetime.now()
                        utterance_model_name = _selected_model_name
                        utterance_wants_textarea = _system_to_textarea if is_system else _mic_to_textarea
                        utterance_wants_daily_log = _daily_log_active

                    if in_speech:
                        utterance_buf.extend(data)
                        utterance_frames += 1

                        closed_by_silence = silence_run >= hangover_frames
                        closed_by_length = utterance_frames >= max_utterance_frames

                        if closed_by_silence or closed_by_length:
                            in_speech = False
                            voiced_run = 0
                            silence_run = 0
                            if utterance_frames >= min_utterance_frames:
                                out_queue.put((bytes(utterance_buf), channels, rate, utterance_start_dt, datetime.now(), is_system, utterance_model_name, utterance_wants_textarea, utterance_wants_daily_log))
                                if utterance_wants_daily_log:
                                    _daily_log_queue_incr()
                            utterance_buf = bytearray()
                            utterance_frames = 0

                    # Periodically confirm the device we're reading from is
                    # still the OS default. This is a cheap enumeration call,
                    # so it's throttled to once every DEVICE_CHECK_SECONDS
                    # rather than every frame.
                    frames_since_check += 1
                    if frames_since_check >= device_check_every:
                        frames_since_check = 0
                        try:
                            current = _resolve_capture_device(p, pyaudio, is_system)
                            if current['index'] != device_index or current['name'] != device_name:
                                device_changed = True
                                break
                        except Exception:
                            _log_error(f'{label}: recheck device')
            finally:
                try:
                    stream.stop_stream()
                    stream.close()
                except Exception:
                    pass

            if device_changed:
                # A headset swap mid-sentence shouldn't just discard those
                # words — flush whatever was buffered on the old device
                # before reconnecting, exactly like a normal stop would.
                if in_speech and utterance_frames >= min_utterance_frames:
                    out_queue.put((bytes(utterance_buf), channels, rate, utterance_start_dt, datetime.now(), is_system, utterance_model_name, utterance_wants_textarea, utterance_wants_daily_log))
                    if utterance_wants_daily_log:
                        _daily_log_queue_incr()
                in_speech = False
                voiced_run = 0
                silence_run = 0
                utterance_buf = bytearray()
                utterance_frames = 0
                # loop back around: outer while re-resolves and reopens
                # against whatever is now the default device

        # Streaming has stopped (the outer while loop exits as soon as
        # stop_event is set) — but if we were mid-utterance at that exact
        # moment, don't throw away what was already captured. "Turn off"
        # means stop listening for more speech, not discard what was
        # already heard, so flush whatever's buffered to the queue and let
        # the worker thread transcribe it like any other utterance.
        if in_speech and utterance_frames >= min_utterance_frames:
            out_queue.put((bytes(utterance_buf), channels, rate, utterance_start_dt, datetime.now(), is_system, utterance_model_name, utterance_wants_textarea, utterance_wants_daily_log))
            if utterance_wants_daily_log:
                _daily_log_queue_incr()

    except Exception:
        _log_error(f'{label}: setup')
    finally:
        if p is not None:
            _pa_release()
        _com_thread_uninit()


# ── Single global transcription worker (merges mic + system in order) ──
# Mic and system audio are two independently-running VAD segmenters, so an
# utterance that STARTED earlier is not guaranteed to be QUEUED first — a
# short utterance on one source can finish and land in its queue well
# before a long, still-in-progress utterance on the other source that
# actually began speaking earlier. Two separate worker threads (the old
# design) made this worse: even if queuing order were correct, whichever
# thread's Whisper call happened to FINISH first would win the race to
# append to the shared transcript, regardless of which utterance started
# first.
#
# _merge_worker_loop fixes both problems by being the only thing that
# transcribes: a single thread that looks at the oldest pending item from
# each queue and only ever processes the one with the earlier start_dt.
# If only one queue currently has something, that item is only "safe" to
# process once it's older than _MAX_QUEUE_DELAY — the worst-case time an
# utterance can sit being captured before _capture_loop is forced to flush
# it (VAD_MAX_UTTERANCE_SECONDS + VAD_HANGOVER_SECONDS, plus a small
# safety margin). Until then, the other source could still, in principle,
# produce an utterance that started even earlier, so the lone item waits.
# In normal back-and-forth conversation both queues have something to
# compare almost all the time, so this rarely adds any real latency — the
# worst case only shows up after one source goes completely silent for a
# stretch.
#
# Runs for the whole lifetime of the process (started once, from
# on_load(), like _warm_model) rather than being tied to Mic/System being
# toggled on/off — it simply idles when both queues are empty, and keeps
# draining whatever's left even after a source is switched off, so
# nothing already captured is ever discarded.
_MAX_QUEUE_DELAY = VAD_MAX_UTTERANCE_SECONDS + VAD_HANGOVER_SECONDS + 0.5


def _dequeue_nowait(q):
    try:
        return q.get_nowait()
    except queue.Empty:
        return None


def _merge_worker_loop():
    mic_head = None  # a dequeued-but-not-yet-processed item from _mic_queue, or None
    sys_head = None  # same, for _system_queue

    while True:
        if mic_head is None:
            mic_head = _dequeue_nowait(_mic_queue)
        if sys_head is None:
            sys_head = _dequeue_nowait(_system_queue)

        if mic_head is not None and sys_head is not None:
            # Both sides have something waiting — direct comparison is
            # unambiguous, no need to wait.
            if mic_head[3] <= sys_head[3]:   # index 3 = start_dt
                item, mic_head = mic_head, None
            else:
                item, sys_head = sys_head, None
        elif mic_head is not None or sys_head is not None:
            head = mic_head if mic_head is not None else sys_head
            age = (datetime.now() - head[3]).total_seconds()
            if age >= _MAX_QUEUE_DELAY:
                # Old enough that the other (currently empty) queue could
                # not possibly still produce anything that started earlier.
                item = head
                if mic_head is not None:
                    mic_head = None
                else:
                    sys_head = None
            else:
                time_module.sleep(0.15)
                continue
        else:
            time_module.sleep(0.15)
            continue

        raw, channels, rate, start_dt, end_dt, is_system, model_name, wants_textarea, wants_daily_log = item
        if wants_daily_log:
            _daily_log_queue_decr()
        label = 'system' if is_system else 'mic'
        _set_processing(label, True)
        try:
            _process_and_emit(label, raw, channels, rate, start_dt, end_dt, is_system, model_name, wants_textarea, wants_daily_log)
        except Exception:
            _log_error(f'{label}: merge worker')
        finally:
            _set_processing(label, False)


# ── Thread lifecycle: live mic / system capture ───────────────────────

def _start_mic():
    global _mic_capture_thread, _mic_stop_event
    with _state_lock:
        if _mic_capture_thread and _mic_capture_thread.is_alive():
            return
        _mic_stop_event = threading.Event()
        _mic_capture_thread = threading.Thread(
            target=_capture_loop, args=('mic', _mic_stop_event, False, _mic_queue), daemon=True)
        _mic_capture_thread.start()


def _stop_mic():
    global _mic_capture_thread
    with _state_lock:
        if _mic_stop_event:
            _mic_stop_event.set()
        _mic_capture_thread = None


def _start_system():
    global _system_capture_thread, _system_stop_event
    with _state_lock:
        if _system_capture_thread and _system_capture_thread.is_alive():
            return
        _system_stop_event = threading.Event()
        _system_capture_thread = threading.Thread(
            target=_capture_loop, args=('system', _system_stop_event, True, _system_queue), daemon=True)
        _system_capture_thread.start()


def _stop_system():
    global _system_capture_thread
    with _state_lock:
        if _system_stop_event:
            _system_stop_event.set()
        _system_capture_thread = None


def _sync_mic_capture():
    """Mic capture should be running whenever either consumer wants it:
    the textarea button, or the Daily Log button (which always wants both
    sources). Called after every change to _mic_to_textarea or
    _daily_log_active so the thread state stays correct regardless of
    which one flipped."""
    if _mic_to_textarea or _daily_log_active:
        _start_mic()
    else:
        _stop_mic()


def _sync_system_capture():
    """Same idea as _sync_mic_capture, for system audio. Note this is only
    ever invoked from a Flask request thread (a button click) — see the
    WASAPI crash-safety comment near _system_to_textarea above — never from
    on_load()."""
    if _system_to_textarea or _daily_log_active:
        _start_system()
    else:
        _stop_system()


# ── Mic -> WAV recording (independent of the live transcription above) ─

def _next_recording_path():
    """current-date.wav, or current-date_v2.wav / _v3 / ... on conflict.
    Saved directly in Audio Notes Data, alongside content.txt."""
    base = datetime.now().strftime('%Y-%m-%d')
    candidate = os.path.join(DATA_FOLDER, base + '.wav')
    if not os.path.exists(candidate):
        return candidate
    version = 2
    while True:
        candidate = os.path.join(DATA_FOLDER, f'{base}_v{version}.wav')
        if not os.path.exists(candidate):
            return candidate
        version += 1


# ── Recording mixer config ──────────────────────────────────────────
# Mic and system-loopback are two independently-clocked audio devices, so
# they can't just be interleaved — each gets its own reader thread that
# continuously reads + resamples to a shared canonical rate/mono format and
# pushes fixed-size chunks into its own queue; a mixer then sums whatever's
# available from both queues on each tick and writes the combined signal to
# the WAV file. Missing/lagging audio from one side is padded with silence
# for that tick rather than stalling the whole recording.
RECORD_MIX_RATE = 48000
RECORD_FRAME_SECONDS = 0.05
RECORD_FRAME_SAMPLES = int(RECORD_MIX_RATE * RECORD_FRAME_SECONDS)


def _record_reader_loop(label, stop_event, is_system, out_queue):
    """Continuously reads one device (mic or system loopback), resamples to
    RECORD_MIX_RATE mono, and pushes chunks to out_queue for the mixer.
    Same as _capture_loop, this re-checks the OS default device periodically
    and reconnects if it changes (e.g. a headset switch mid-recording),
    instead of staying pinned to whichever device was default when Record
    was first clicked."""
    _com_thread_init()
    try:
        import pyaudiowpatch as pyaudio
        import numpy as np
    except ImportError:
        _log_error(f'{label}: pyaudiowpatch/numpy is not installed')
        _com_thread_uninit()
        return

    p = None
    try:
        p = _pa_acquire()

        while not stop_event.is_set():
            try:
                device_info = _resolve_capture_device(p, pyaudio, is_system)
            except Exception:
                _log_error(f'{label}: resolve device')
                time_module.sleep(1.0)
                continue

            device_index = device_info['index']
            device_name = device_info['name']
            channels = max(1, int(device_info['maxInputChannels']))
            rate = int(device_info['defaultSampleRate'])
            frames_per_buffer = max(160, int(rate * RECORD_FRAME_SECONDS))

            try:
                stream = p.open(
                    format=pyaudio.paInt16,
                    channels=channels,
                    rate=rate,
                    input=True,
                    frames_per_buffer=frames_per_buffer,
                    input_device_index=device_index,
                )
            except Exception:
                _log_error(f'{label}: open stream')
                time_module.sleep(1.0)
                continue

            frame_seconds_actual = frames_per_buffer / float(rate)
            device_check_every = max(1, int(DEVICE_CHECK_SECONDS / frame_seconds_actual))
            frames_since_check = 0

            try:
                while not stop_event.is_set():
                    try:
                        data = stream.read(frames_per_buffer, exception_on_overflow=False)
                    except Exception:
                        _log_error(f'{label}: stream read')
                        time_module.sleep(0.3)
                        continue

                    samples = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
                    if channels > 1:
                        samples = samples.reshape(-1, channels).mean(axis=1)
                    samples = _resample_linear(samples, rate, RECORD_MIX_RATE)
                    out_queue.put(samples)

                    frames_since_check += 1
                    if frames_since_check >= device_check_every:
                        frames_since_check = 0
                        try:
                            current = _resolve_capture_device(p, pyaudio, is_system)
                            if current['index'] != device_index or current['name'] != device_name:
                                break  # device changed — reconnect to the new default
                        except Exception:
                            _log_error(f'{label}: recheck device')
            finally:
                try:
                    stream.stop_stream()
                    stream.close()
                except Exception:
                    pass

    except Exception:
        _log_error(f'{label}: setup')
    finally:
        if p is not None:
            _pa_release()
        _com_thread_uninit()


def _record_mixer_loop(stop_event, mic_queue, sys_queue, filepath):
    """Sums whatever's available from the mic and system-audio queues each
    tick and writes it to a single mono WAV file. Keeps running after
    stop_event is set until both queues are drained, so nothing already
    captured gets cut off."""
    import numpy as np

    wf = None
    try:
        wf = wave.open(filepath, 'wb')
        wf.setnchannels(1)
        wf.setsampwidth(2)  # 16-bit PCM
        wf.setframerate(RECORD_MIX_RATE)

        while True:
            if stop_event.is_set() and mic_queue.empty() and sys_queue.empty():
                break

            try:
                mic_chunk = mic_queue.get(timeout=RECORD_FRAME_SECONDS)
            except queue.Empty:
                mic_chunk = None
            try:
                sys_chunk = sys_queue.get_nowait()
            except queue.Empty:
                sys_chunk = None

            if mic_chunk is None and sys_chunk is None:
                continue

            length = max(
                len(mic_chunk) if mic_chunk is not None else 0,
                len(sys_chunk) if sys_chunk is not None else 0,
                RECORD_FRAME_SAMPLES,
            )
            mixed = np.zeros(length, dtype=np.float32)
            if mic_chunk is not None:
                mixed[:len(mic_chunk)] += mic_chunk
            if sys_chunk is not None:
                mixed[:len(sys_chunk)] += sys_chunk
            np.clip(mixed, -1.0, 1.0, out=mixed)

            pcm16 = (mixed * 32767).astype(np.int16)
            wf.writeframes(pcm16.tobytes())
    except Exception:
        # Logged here (with the full traceback) rather than only by the
        # caller, so if this is why a recording ends up as a near-empty
        # file, the exact cause is in audio_notes_error.log.
        _log_error('record: mixer')
        raise
    finally:
        if wf is not None:
            try:
                wf.close()
            except Exception:
                _log_error('record: mixer close')


def _record_loop(stop_event, filepath):
    """Orchestrates a combined mic + system-audio recording: spins up a
    reader thread per source and runs the mixer (which writes the file)
    on this thread until both sources are stopped and drained."""
    mic_queue = queue.Queue()
    sys_queue = queue.Queue()

    mic_reader = threading.Thread(
        target=_record_reader_loop, args=('record-mic', stop_event, False, mic_queue), daemon=True)
    sys_reader = threading.Thread(
        target=_record_reader_loop, args=('record-system', stop_event, True, sys_queue), daemon=True)
    mic_reader.start()
    sys_reader.start()

    try:
        _record_mixer_loop(stop_event, mic_queue, sys_queue, filepath)
    except Exception:
        pass  # already logged inside _record_mixer_loop, with full detail
    finally:
        mic_reader.join(timeout=2)
        sys_reader.join(timeout=2)


def _start_recording():
    global _recording_thread, _recording_stop_event, _recording_path, _recording_started_at
    with _state_lock:
        if _recording_thread and _recording_thread.is_alive():
            return
        _recording_path = _next_recording_path()
        _recording_started_at = time_module.time()
        _recording_stop_event = threading.Event()
        _recording_thread = threading.Thread(
            target=_record_loop, args=(_recording_stop_event, _recording_path), daemon=True)
        _recording_thread.start()


def _stop_recording():
    """Signals the recording to stop and waits (bounded) for it to actually
    finish, instead of just flipping a flag and hoping — that was the bug
    behind "clicking stop doesn't stop it": the thread reference was
    discarded immediately, so nothing ever confirmed the background
    reader/mixer threads (and the WAV file) had actually closed, and a
    stuck stream.read() could keep the file open — and growing or stuck at
    0 bytes — indefinitely with the UI already showing "stopped"."""
    global _recording_thread, _recording_started_at
    with _state_lock:
        thread = _recording_thread
        stop_event = _recording_stop_event
        _recording_thread = None
        _recording_started_at = None

    if stop_event:
        stop_event.set()

    if thread is not None:
        thread.join(timeout=5)
        if thread.is_alive():
            _log_error(
                'record: stop timed out — recording thread did not exit '
                'within 5s; the audio device read may be stuck (this can '
                'happen if another stream is holding the same WASAPI '
                'loopback device). The WAV file may still be open.'
            )
            raise RuntimeError('Recording did not stop within 5 seconds.')


# ── Page ─────────────────────────────────────────────────────────────

@bp.route('/audio_notes')
def audio_notes_view():
    _ensure_boot_capture_started()
    with _lock:
        content = _content
    return render_template(
        'audio_notes.html',
        content=content,
        mic_active=_mic_to_textarea,
        system_active=_system_to_textarea,
        recording_active=_recording_active,
        daily_log_enabled=_daily_log_active,
        available_models=AVAILABLE_MODELS,
        current_model=_selected_model_name,
    )


# ── API ──────────────────────────────────────────────────────────────

@bp.route('/api/audio_notes/poll')
def poll_route():
    global _pending_chunks
    _ensure_boot_capture_started()
    with _lock:
        pending = ''.join(_pending_chunks)
        _pending_chunks = []
    return jsonify({
        'pending': pending,
        'mic_active': _mic_to_textarea,
        'system_active': _system_to_textarea,
        'mic_processing': _mic_processing,
        'system_processing': _system_processing,
        'mic_queue_depth': _mic_queue.qsize(),
        'system_queue_depth': _system_queue.qsize(),
        'model_loading': _model_loading,
        'current_model': _selected_model_name,
        'daily_log_enabled': _daily_log_active,
        'daily_log_queue_depth': _daily_log_queue_depth,
        'recording_active': _recording_active,
        'recording_started_at': (int(_recording_started_at * 1000) if _recording_active and _recording_started_at else None),
        'recording_file': (os.path.basename(_recording_path) if _recording_active and _recording_path else None),
    })


@bp.route('/api/audio_notes/model', methods=['POST'])
def set_model_route():
    global _selected_model_name
    name = (request.json or {}).get('model', '').strip()
    valid_names = {m[0] for m in AVAILABLE_MODELS}
    if name not in valid_names:
        return jsonify({'status': 'error', 'message': 'Unknown model.'})
    # Just record the choice — the (possibly slow, first-download) load
    # happens lazily in _get_model(), the next time something needs to
    # transcribe. The same model is used for both destinations (textarea
    # and Daily Log), since each utterance is only ever transcribed once.
    _selected_model_name = name
    return jsonify({'status': 'ok', 'model': name})


@bp.route('/api/audio_notes/daily_log/toggle', methods=['POST'])
def toggle_daily_log_route():
    """Turns the Daily Log on/off. When on, this makes sure BOTH mic and
    system-audio capture are running (starting whichever isn't already, on
    top of whatever the textarea buttons have independently requested) and
    writes every utterance from either source to the day's .txt file.
    Turning it off only stops the file writes; it leaves the textarea's own
    mic/system streaming exactly as it was."""
    global _daily_log_active
    with _state_lock:
        _daily_log_active = not _daily_log_active
        enabled = _daily_log_active
    try:
        _sync_mic_capture()
        _sync_system_capture()
    except Exception:
        _log_error('daily log toggle')
        with _state_lock:
            _daily_log_active = False
        return jsonify({
            'status': 'error',
            'enabled': False,
            'message': 'Could not start capture for the Daily Notes log. '
                       'Make sure pyaudiowpatch and faster-whisper are '
                       'installed and both a microphone and a WASAPI '
                       'output/loopback device are available.',
        })
    return jsonify({'status': 'ok', 'enabled': enabled})


@bp.route('/api/audio_notes/save', methods=['POST'])
def save_route():
    global _content
    text = (request.json or {}).get('content', '')
    with _lock:
        _content = text
        _save_content(_content)
    return jsonify({'status': 'ok'})


@bp.route('/api/audio_notes/mic/toggle', methods=['POST'])
def toggle_mic_route():
    """Turns mic streaming into the textarea on/off. Mic capture itself
    keeps running underneath if the Daily Log button still wants it — see
    _sync_mic_capture."""
    global _mic_to_textarea
    with _state_lock:
        _mic_to_textarea = not _mic_to_textarea
        active = _mic_to_textarea
    try:
        _sync_mic_capture()
    except Exception:
        _log_error('mic toggle')
        with _state_lock:
            _mic_to_textarea = False
        return jsonify({
            'status': 'error',
            'active': False,
            'message': 'Could not start microphone capture. Make sure '
                       'pyaudiowpatch and faster-whisper are installed and '
                       'a microphone is available.',
        })
    return jsonify({'status': 'ok', 'active': active})


@bp.route('/api/audio_notes/system/toggle', methods=['POST'])
def toggle_system_route():
    """Turns system-audio streaming into the textarea on/off. System
    capture itself keeps running underneath if the Daily Log button still
    wants it — see _sync_system_capture."""
    global _system_to_textarea
    with _state_lock:
        _system_to_textarea = not _system_to_textarea
        active = _system_to_textarea
    try:
        _sync_system_capture()
    except Exception:
        _log_error('system toggle')
        with _state_lock:
            _system_to_textarea = False
        return jsonify({
            'status': 'error',
            'active': False,
            'message': 'Could not start system-audio capture. Make sure '
                       'pyaudiowpatch and faster-whisper are installed and '
                       'a WASAPI output/loopback device is available.',
        })
    return jsonify({'status': 'ok', 'active': active})


@bp.route('/api/audio_notes/record/toggle', methods=['POST'])
def toggle_record_route():
    global _recording_active
    with _state_lock:
        _recording_active = not _recording_active
        active = _recording_active
    try:
        if active:
            _start_recording()
        else:
            _stop_recording()
    except Exception:
        _log_error('record toggle')
        with _state_lock:
            _recording_active = False
        message = (
            'Could not start recording. Make sure pyaudiowpatch is '
            'installed and both a microphone and a WASAPI output/loopback '
            'device are available.'
            if active else
            'Recording did not stop cleanly within 5 seconds — the audio '
            'device may be stuck. Check audio_notes_error.log; the WAV '
            'file may still be open until the process is restarted.'
        )
        return jsonify({'status': 'error', 'active': False, 'message': message})
    return jsonify({
        'status': 'ok',
        'active': active,
        'file': os.path.basename(_recording_path) if active and _recording_path else None,
    })


@bp.route('/api/audio_notes/transcribe_file', methods=['POST'])
def transcribe_file_route():
    """Accepts a dropped/uploaded audio or video file, transcribes it in one
    shot, and returns the text — the file itself is discarded afterwards
    (this is a one-off "get the script out of this file" tool, not storage;
    use the Record button if you want the audio itself kept)."""
    f = request.files.get('file')
    if f is None or not f.filename:
        return jsonify({'status': 'error', 'message': 'No file received.'})

    ext = os.path.splitext(f.filename)[1].lower()
    if not (1 < len(ext) <= 6 and ext[1:].isalnum()):
        ext = ''
    tmp_path = os.path.join(UPLOADS_TMP_FOLDER, uuid.uuid4().hex + ext)

    try:
        f.save(tmp_path)
        text = _transcribe_file(tmp_path)
        if not text:
            return jsonify({'status': 'error', 'message': 'No speech was detected in that file.'})
        return jsonify({'status': 'ok', 'text': text})
    except Exception:
        _log_error('transcribe_file')
        return jsonify({
            'status': 'error',
            'message': 'Could not transcribe that file. .wav files are '
                       'decoded directly and need no extra dependencies, so '
                       'if this was a .wav, check audio_notes_error.log for '
                       'the real cause. Other formats (mp4, mp3, m4a, mov, '
                       'etc.) are decoded via faster-whisper\'s "av" '
                       'dependency, which needs to be installed separately '
                       '(pip install av) for those to work.',
        })
    finally:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
