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

WHISPER_DEVICE = 'cpu'
WHISPER_COMPUTE_TYPE = 'int8'
FILE_TRANSCRIBE_BEAM_SIZE = 5     # uploaded files are offline/batch, so it's worth spending more compute for quality

# Models selectable from the dropdown in the UI. distil-* and *.en models are
# English-only. Roughly fastest/roughest -> slowest/most-accurate on CPU.
AVAILABLE_MODELS = [
    ('tiny.en', 'Tiny (English) — default, fastest, roughest'),
    ('base', 'Base — fast Whisper'),
    ('small', 'Small — balanced, multilingual'),
    ('distil-small.en', 'Distil Small (English) — fast + better than base'),
    ('distil-medium.en', 'Distil Medium (English) — balanced'),
    ('medium', 'Medium — slower, multilingual'),
]
DEFAULT_MODEL = 'tiny.en'

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

# Live capture: a persistent queue per stream decouples audio reading from
# transcription (see _capture_loop / _worker_loop below) — this is the fix
# for audio being dropped while a chunk is transcribing.
#
# Mic defaults to ON (auto-started in on_load() below) — this also drives
# the Daily Log file: rather than a separate dedicated capture pipeline,
# the daily file is just an extra output of this exact same interactive
# capture path (see _emit_transcript below).
#
# System Audio defaults to OFF and must be started with a manual click.
# This is deliberate: auto-starting WASAPI loopback from a background
# thread at app boot has twice caused a hard process crash (an
# unrecoverable native access violation, exit code -1073741819 /
# 0xC0000005 — no Python traceback, since it's not a catchable exception)
# — once via a dedicated daily system-audio thread, and again when System
# was simply auto-started the same way Mic is here. Mic-only auto-start has
# never triggered this. A manual click (which runs from a Flask request
# thread, not at startup) has been reliably fine, same as Record already
# works. If you want System Audio running continuously, just click it once
# after the app starts.
_mic_active = True
_mic_capture_thread = None
_mic_worker_thread = None
_mic_stop_event = None
_mic_queue = queue.Queue()

_system_active = False
_system_capture_thread = None
_system_worker_thread = None
_system_stop_event = None
_system_queue = queue.Queue()

_mic_processing = False       # True while a mic utterance is actively being transcribed
_system_processing = False    # True while a system-audio utterance is actively being transcribed

# Daily Log (Audio Notes Data/Daily Notes/YYYY-MM-DD.txt) can be paused
# independently of the Mic/System toggles above — turning this off still
# lets live transcription keep flowing into the on-screen textarea, it just
# stops appending timestamped blocks to the daily .txt file. See
# _emit_transcript, which is the single place both destinations are fed.
_daily_log_enabled = True

# Mic -> WAV recording (independent of live transcription capture above)
_recording_active = False
_recording_thread = None
_recording_stop_event = None
_recording_path = None
_recording_started_at = None   # epoch seconds, for the frontend's elapsed timer

_model = None                          # the currently loaded WhisperModel instance
_model_name = None                     # name of the model that's actually loaded
_selected_model_name = DEFAULT_MODEL   # name the user has chosen via the dropdown
_model_lock = threading.Lock()
_model_loading = False                 # True while a (re)load is in progress


def on_load():
    """Called once by app.py when this module is registered at startup."""
    global _content
    _content = _load_content()
    _cleanup_stale_content_tmp()
    _cleanup_stale_uploads()
    # Only Mic auto-starts here. System Audio deliberately does NOT — see
    # the comment on _system_active above for why (it's the confirmed
    # trigger for a hard process crash when auto-started this way).
    _start_mic()


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


def _emit_transcript(text, start_dt, end_dt, is_system):
    """Append newly-transcribed text to the authoritative content, persist
    it, and queue it so the next frontend poll can append it to the
    on-screen textarea too. Also appends the same text, timestamped and
    tagged by source, to the day's Daily Log file — this is the entire
    "Daily Log" now: not a separate capture pipeline, just an extra output
    of this same, already-proven Mic/System capture path."""
    global _content
    if not text:
        return
    with _lock:
        sep = '' if (not _content or _content[-1:].isspace()) else ' '
        _content += sep + text
        _pending_chunks.append(text)
        _save_content(_content)
    if _daily_log_enabled:
        _append_daily_log(start_dt, end_dt, text, is_system)


# ── Whisper model (lazy — (re)loaded on demand, one instance shared) ──
# Switching the dropdown just updates _selected_model_name; the actual
# (potentially slow, first-download) load happens here, the next time
# something needs to transcribe.

def _get_model():
    global _model, _model_name, _model_loading
    target = _selected_model_name
    if _model is not None and _model_name == target:
        return _model
    with _model_lock:
        target = _selected_model_name  # re-read: may have changed while we waited for the lock
        if _model is not None and _model_name == target:
            return _model
        _model_loading = True
        try:
            from faster_whisper import WhisperModel
            _model = WhisperModel(
                target,
                device=WHISPER_DEVICE,
                compute_type=WHISPER_COMPUTE_TYPE,
            )
            _model_name = target
        finally:
            _model_loading = False
    return _model


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


def _process_and_emit(label, raw_bytes, channels, rate, start_dt, end_dt, is_system):
    """Transcribes one already-VAD-segmented utterance (live mic/system
    capture). Runs on a worker thread, never on the audio-reading thread —
    see _capture_loop / _worker_loop."""
    try:
        import numpy as np
        samples = np.frombuffer(raw_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        if channels > 1:
            samples = samples.reshape(-1, channels).mean(axis=1)
        samples = _resample_linear(samples, rate, 16000)

        model_name = _selected_model_name
        model = _get_model()
        lang = 'en' if _model_is_english_only(model_name) else None

        # condition_on_previous_text=False is the setting recommended for the
        # distil-* checkpoints, to stop them fixating on earlier chunk text.
        # vad_filter=True is a second, finer-grained pass inside Whisper
        # itself — belt-and-braces on top of the utterance-level VAD in
        # _capture_loop, which is what stops silence from reaching here at
        # all (and, for mic audio specifically, catches the ambient-noise
        # false-triggers that would otherwise get hallucinated into short
        # filler words like "Okay." / "Yeah." instead of correctly
        # producing nothing).
        segments, _ = model.transcribe(
            samples,
            language=lang,
            beam_size=1,
            vad_filter=True,
            condition_on_previous_text=False,
        )
        text = ' '.join(seg.text.strip() for seg in segments).strip()
        if text:
            _emit_transcript(text, start_dt, end_dt, is_system)
    except Exception:
        _log_error(f'{label}: transcribe')


def _daily_log_path_for(dt):
    return os.path.join(DAILY_NOTES_FOLDER, dt.strftime('%Y-%m-%d') + '.txt')


def _append_daily_log(start_dt, end_dt, text, is_system):
    """Appends one timestamp-bracketed block to the day's log file (the file
    for the day the utterance STARTED in, so something spanning midnight
    still lands under the day it began). Format:
    [HH:MM:SS] [Mic/Sys] transcribed text [HH:MM:SS]
    marking when that block of speech started and ended, and whether it
    came from the microphone or from system audio."""
    if not text:
        return
    path = _daily_log_path_for(start_dt)
    tag = 'Sys' if is_system else 'Mic'
    line = f'[{start_dt.strftime("%H:%M:%S")}] [{tag}] {text} [{end_dt.strftime("%H:%M:%S")}]\n'
    try:
        with open(path, 'a', encoding='utf-8') as f:
            f.write(line)
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
    model = _get_model()
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
# a separate _worker_loop thread drains that queue and does the actual (slow)
# transcription. If transcription temporarily falls behind, utterances queue
# up rather than audio getting silently dropped by the OS-level stream buffer
# overflowing — a lagging transcript, never lost speech.

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
    stream = None
    try:
        p = pyaudio.PyAudio()

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

        channels = max(1, int(device_info['maxInputChannels']))
        rate = int(device_info['defaultSampleRate'])
        frames_per_buffer = max(160, int(rate * FRAME_SECONDS))

        stream = p.open(
            format=pyaudio.paInt16,
            channels=channels,
            rate=rate,
            input=True,
            frames_per_buffer=frames_per_buffer,
            input_device_index=device_info['index'],
        )

        # VAD state
        frame_seconds_actual = frames_per_buffer / float(rate)
        hangover_frames = max(1, int(VAD_HANGOVER_SECONDS / frame_seconds_actual))
        max_utterance_frames = max(1, int(max_utterance_seconds / frame_seconds_actual))
        min_utterance_frames = max(1, int(VAD_MIN_UTTERANCE_SECONDS / frame_seconds_actual))

        noise_floor = VAD_MIN_ABS_RMS
        voiced_run = 0
        silence_run = 0
        in_speech = False
        utterance_buf = bytearray()
        utterance_frames = 0
        utterance_start_dt = None

        while not stop_event.is_set():
            try:
                data = stream.read(frames_per_buffer, exception_on_overflow=False)
            except Exception:
                _log_error(f'{label}: stream read')
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
                # Only chase the noise floor while we're confident we're NOT
                # in speech, so a long utterance can't drag the floor up and
                # make the VAD deaf to quieter follow-on speech.
                if not in_speech:
                    noise_floor = noise_floor * 0.98 + rms * 0.02

            if not in_speech and voiced_run >= VAD_ENTER_FRAMES:
                in_speech = True
                utterance_buf = bytearray()
                utterance_frames = 0
                utterance_start_dt = datetime.now()

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
                        out_queue.put((bytes(utterance_buf), channels, rate, utterance_start_dt, datetime.now(), is_system))
                    utterance_buf = bytearray()
                    utterance_frames = 0

        # Streaming has stopped (the while loop above exits as soon as
        # stop_event is set) — but if we were mid-utterance at that exact
        # moment, don't throw away what was already captured. "Turn off"
        # means stop listening for more speech, not discard what was
        # already heard, so flush whatever's buffered to the queue and let
        # the worker thread transcribe it like any other utterance.
        if in_speech and utterance_frames >= min_utterance_frames:
            out_queue.put((bytes(utterance_buf), channels, rate, utterance_start_dt, datetime.now(), is_system))

    except Exception:
        _log_error(f'{label}: setup')
    finally:
        try:
            if stream is not None:
                stream.stop_stream()
                stream.close()
        except Exception:
            pass
        try:
            if p is not None:
                p.terminate()
        except Exception:
            pass
        _com_thread_uninit()


def _worker_loop(label, stop_event, in_queue):
    """Drains in_queue and transcribes each utterance in order. Keeps running
    (even after stop_event is set) until the queue is empty, so toggling a
    stream off never throws away audio that was already captured — it just
    finishes catching up on what's left, then exits."""
    while True:
        try:
            raw, channels, rate, start_dt, end_dt, is_system = in_queue.get(timeout=0.25)
        except queue.Empty:
            if stop_event.is_set():
                break
            continue

        _set_processing(label, True)
        try:
            _process_and_emit(label, raw, channels, rate, start_dt, end_dt, is_system)
        except Exception:
            _log_error(f'{label}: worker loop')
        finally:
            _set_processing(label, False)
            in_queue.task_done()

    _set_processing(label, False)


# ── Thread lifecycle: live mic / system capture ───────────────────────

def _start_mic():
    global _mic_capture_thread, _mic_worker_thread, _mic_stop_event
    with _state_lock:
        if _mic_capture_thread and _mic_capture_thread.is_alive():
            return
        _mic_stop_event = threading.Event()
        _mic_capture_thread = threading.Thread(
            target=_capture_loop, args=('mic', _mic_stop_event, False, _mic_queue), daemon=True)
        _mic_worker_thread = threading.Thread(
            target=_worker_loop, args=('mic', _mic_stop_event, _mic_queue), daemon=True)
        _mic_capture_thread.start()
        _mic_worker_thread.start()


def _stop_mic():
    global _mic_capture_thread
    with _state_lock:
        if _mic_stop_event:
            _mic_stop_event.set()
        _mic_capture_thread = None


def _start_system():
    global _system_capture_thread, _system_worker_thread, _system_stop_event
    with _state_lock:
        if _system_capture_thread and _system_capture_thread.is_alive():
            return
        _system_stop_event = threading.Event()
        _system_capture_thread = threading.Thread(
            target=_capture_loop, args=('system', _system_stop_event, True, _system_queue), daemon=True)
        _system_worker_thread = threading.Thread(
            target=_worker_loop, args=('system', _system_stop_event, _system_queue), daemon=True)
        _system_capture_thread.start()
        _system_worker_thread.start()


def _stop_system():
    global _system_capture_thread
    with _state_lock:
        if _system_stop_event:
            _system_stop_event.set()
        _system_capture_thread = None


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
    RECORD_MIX_RATE mono, and pushes chunks to out_queue for the mixer."""
    _com_thread_init()
    try:
        import pyaudiowpatch as pyaudio
        import numpy as np
    except ImportError:
        _log_error(f'{label}: pyaudiowpatch/numpy is not installed')
        _com_thread_uninit()
        return

    p = None
    stream = None
    try:
        p = pyaudio.PyAudio()

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

        channels = max(1, int(device_info['maxInputChannels']))
        rate = int(device_info['defaultSampleRate'])
        frames_per_buffer = max(160, int(rate * RECORD_FRAME_SECONDS))

        stream = p.open(
            format=pyaudio.paInt16,
            channels=channels,
            rate=rate,
            input=True,
            frames_per_buffer=frames_per_buffer,
            input_device_index=device_info['index'],
        )

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

    except Exception:
        _log_error(f'{label}: setup')
    finally:
        try:
            if stream is not None:
                stream.stop_stream()
                stream.close()
        except Exception:
            pass
        try:
            if p is not None:
                p.terminate()
        except Exception:
            pass
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
    with _lock:
        content = _content
    return render_template(
        'audio_notes.html',
        content=content,
        mic_active=_mic_active,
        system_active=_system_active,
        recording_active=_recording_active,
        daily_log_enabled=_daily_log_enabled,
        available_models=AVAILABLE_MODELS,
        current_model=_selected_model_name,
    )


# ── API ──────────────────────────────────────────────────────────────

@bp.route('/api/audio_notes/poll')
def poll_route():
    global _pending_chunks
    with _lock:
        pending = ' '.join(_pending_chunks)
        _pending_chunks = []
    return jsonify({
        'pending': pending,
        'mic_active': _mic_active,
        'system_active': _system_active,
        'mic_processing': _mic_processing,
        'system_processing': _system_processing,
        'mic_queue_depth': _mic_queue.qsize(),
        'system_queue_depth': _system_queue.qsize(),
        'model_loading': _model_loading,
        'current_model': _selected_model_name,
        'daily_log_enabled': _daily_log_enabled,
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
    # transcribe.
    _selected_model_name = name
    return jsonify({'status': 'ok', 'model': name})


@bp.route('/api/audio_notes/daily_log/toggle', methods=['POST'])
def toggle_daily_log_route():
    """Pauses/resumes appending to the Daily Notes .txt file only. Live
    transcription into the textarea (Mic/System toggles) is unaffected."""
    global _daily_log_enabled
    with _state_lock:
        _daily_log_enabled = not _daily_log_enabled
        enabled = _daily_log_enabled
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
    global _mic_active
    with _state_lock:
        _mic_active = not _mic_active
        active = _mic_active
    try:
        if active:
            _start_mic()
        else:
            _stop_mic()
    except Exception:
        _log_error('mic toggle')
        with _state_lock:
            _mic_active = False
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
    global _system_active
    with _state_lock:
        _system_active = not _system_active
        active = _system_active
    try:
        if active:
            _start_system()
        else:
            _stop_system()
    except Exception:
        _log_error('system toggle')
        with _state_lock:
            _system_active = False
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
