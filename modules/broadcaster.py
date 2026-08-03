"""
Broadcaster module — a small OBS replacement. A scene is simply an ordered
list of positioned "elements" (boxes) — each pointing at the camera, the
desktop, a specific open window (e.g. just Excel or PowerPoint instead of
the whole screen), or an uploaded material image. There's no separate
"background" concept: stretch a box to cover the whole frame and it acts
as one; leave it small and it's picture-in-picture. Later elements in the
list draw on top of earlier ones. Output goes to a virtual camera so any
app (Teams, etc.) can pick it up as a webcam, plus a sound board (applause,
censor beep, ...) that plays through your own speakers AND injects into
your mic feed at the same time.

── Hardware/driver requirements (none of this is optional — see comments
   at each import site for why) ───────────────────────────────────────
  pip install opencv-python pyvirtualcam mss sounddevice numpy pywin32 --break-system-packages

  - Virtual camera: Windows cannot create a camera device from pure Python.
    pyvirtualcam needs an actual driver present. This module targets
    "Unity Capture" (https://github.com/schellingb/UnityCapture) — a small
    driver-only install, NOT an application you run, specifically built as
    an OBS-less virtual cam backend. Install it once, then this module's
    output just works. If you'd rather use OBS's own virtual cam driver
    instead, there's no in-app switch for that (it's a "how you set this
    machine up once" choice, not something you'd flip day to day) — set
    "virtual_cam_backend": "obs" by hand in Broadcaster Data/config.json.
  - Mic injection for the sound board: same story but for audio — needs
    "VB-Cable" (https://vb-audio.com/Cable/) installed once. Set it as
    your microphone in Teams/etc. Note VB-Cable only has one input, so
    this plays sound-board effects INTO that virtual cable — it does not
    mix them with your real physical microphone. Mixing both into one
    virtual mic needs something like VoiceMeeter, which is out of scope
    here.
  - Single-window capture: grabbing just the screen region a window
    occupies (the naive approach) would show whatever's actually on top
    at that spot if another window overlaps it — not what "just Excel"
    means. Real per-window capture needs Windows' own PrintWindow API,
    which is what pywin32 provides.

Drop this file in modules/ to enable the Broadcaster tab in the sidebar;
rename it with a leading underscore to disable it.
"""
import glob
import json
import os
import re
import threading
import time as time_module
import uuid
from datetime import datetime

from flask import Blueprint, Response, render_template, request, jsonify, send_from_directory

# ── Module metadata (read by app.py's auto-discovery) ────────────────
NAV_LABEL = 'Broadcaster'
NAV_PATH = '/broadcaster'
ORDER = 70

bp = Blueprint('broadcaster', __name__)

# ── SETUP — small fixed defaults, editable here or via the in-app
#    Setup section (which writes to config.json) ─────────────────────
DEFAULT_OUTPUT_WIDTH = 1280
DEFAULT_OUTPUT_HEIGHT = 720
DEFAULT_OUTPUT_FPS = 30
DEFAULT_TRANSITION_DURATION = 0.4  # seconds
DEFAULT_ELEMENT_SIZE_PCT = 25      # w/h for a freshly-added element box
OVERLAY_BORDER_BGR = (130, 130, 0)  # ~ --teal-700 (#008282) in OpenCV's BGR order
WINDOW_SOURCE_PREFIX = 'window:'    # element['source'] = 'window:<exact window title>'

TRANSITIONS = ('cut', 'crossfade', 'slide-left', 'slide-right')

# Quality presets shown in the simplified Setup UI — (width, height, fps).
# Anything outside these three is still honored if hand-edited into
# config.json; the UI just doesn't need to expose every permutation.
QUALITY_PRESETS = {
    'low': (640, 360, 24),
    'balanced': (1280, 720, 30),
    'high': (1920, 1080, 30),
}

# ── File paths ─────────────────────────────────────────────────────────
DATA_FOLDER = os.path.join(os.getcwd(), 'Broadcaster Data')
MATERIALS_FOLDER = os.path.join(DATA_FOLDER, 'Materials')
SOUNDS_FOLDER = os.path.join(DATA_FOLDER, 'Sounds')
RECORDINGS_FOLDER = os.path.join(DATA_FOLDER, 'Recordings')
os.makedirs(MATERIALS_FOLDER, exist_ok=True)
os.makedirs(SOUNDS_FOLDER, exist_ok=True)
os.makedirs(RECORDINGS_FOLDER, exist_ok=True)

CONFIG_FILE = os.path.join(DATA_FOLDER, 'config.json')
SCENES_FILE = os.path.join(DATA_FOLDER, 'scenes.json')
TEMPLATES_FILE = os.path.join(DATA_FOLDER, 'templates.json')
ERROR_LOG = os.path.join(DATA_FOLDER, 'broadcaster_error.log')

_lock = threading.Lock()   # guards config/scenes/templates read-modify-write on disk


def _log_error(context):
    try:
        with open(ERROR_LOG, 'a', encoding='utf-8') as f:
            f.write(f'--- {datetime.now().isoformat()} [{context}] ---\n')
            import traceback
            f.write(traceback.format_exc())
            f.write('\n')
    except Exception:
        pass


# ── Atomic JSON persistence (same pattern as timers.py / time.py) ─────

def _load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return default
    return default


def _save_json(path, data):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def _default_config():
    return {
        'camera_device_index': 0,
        'monitor_index': 1,
        'virtual_cam_backend': 'unitycapture',   # 'unitycapture' | 'obs' — hand-edit only, see module docstring
        'output_width': DEFAULT_OUTPUT_WIDTH,
        'output_height': DEFAULT_OUTPUT_HEIGHT,
        'output_fps': DEFAULT_OUTPUT_FPS,
        'mic_inject_device_index': None,          # None = auto-detect "CABLE Input"
        'mirror_camera': True,                    # horizontally flip the camera source before compositing
    }


def _load_config():
    cfg = _default_config()
    cfg.update(_load_json(CONFIG_FILE, {}))
    return cfg


def _save_config(cfg):
    with _lock:
        _save_json(CONFIG_FILE, cfg)


def _clean_element(el):
    """Clamps a layout element to sane bounds. `source` is 'camera',
    'desktop', 'window:<title>', or a material filename. A box stretched
    to (0,0)-(100,100) is how a "background" is expressed — there's no
    separate concept for it, it's just a box like any other."""
    return {
        'id': el.get('id') or uuid.uuid4().hex[:8],
        'source': el.get('source', 'camera'),
        'x_pct': max(0.0, min(99.0, float(el.get('x_pct', 10)))),
        'y_pct': max(0.0, min(99.0, float(el.get('y_pct', 10)))),
        'w_pct': max(5.0, min(100.0, float(el.get('w_pct', DEFAULT_ELEMENT_SIZE_PCT)))),
        'h_pct': max(5.0, min(100.0, float(el.get('h_pct', DEFAULT_ELEMENT_SIZE_PCT)))),
        'border': bool(el.get('border', True)),
    }


def _migrate_scene(s):
    """Brings any older scenes.json shape up to the current one (a plain
    'elements' list, nothing else special) — see the module history for
    the two previous shapes this converts from. Nothing saved before this
    change is lost — it's just re-expressed as boxes."""
    elements = list(s.get('elements') or [])

    co = s.pop('camera_overlay', None)
    if co and co.get('enabled'):
        size_pct = co.get('size_pct', DEFAULT_ELEMENT_SIZE_PCT)
        h_pct = size_pct * 9 / 16
        corner = co.get('corner', 'bottom-right')
        margin = 2
        if corner == 'bottom-right':
            x, y = 100 - size_pct - margin, 100 - h_pct - margin
        elif corner == 'bottom-left':
            x, y = margin, 100 - h_pct - margin
        elif corner == 'top-right':
            x, y = 100 - size_pct - margin, margin
        else:
            x, y = margin, margin
        elements.append({'source': 'camera', 'x_pct': x, 'y_pct': y, 'w_pct': size_pct, 'h_pct': h_pct})

    old_type = s.pop('type', None)
    old_material = s.pop('material', None)
    if old_type == 'desktop':
        elements.insert(0, {'source': 'desktop', 'x_pct': 0, 'y_pct': 0, 'w_pct': 100, 'h_pct': 100, 'border': False})
    elif old_type == 'material' and old_material:
        elements.insert(0, {'source': old_material, 'x_pct': 0, 'y_pct': 0, 'w_pct': 100, 'h_pct': 100, 'border': False})

    s['elements'] = [_clean_element(e) for e in elements]
    return s


def _load_scenes():
    return [_migrate_scene(s) for s in _load_json(SCENES_FILE, [])]


def _save_scenes(scenes):
    with _lock:
        _save_json(SCENES_FILE, scenes)


def _find_scene(scenes, scene_id):
    for s in scenes:
        if s['id'] == scene_id:
            return s
    return None


def _load_templates():
    return _load_json(TEMPLATES_FILE, [])


def _save_templates(templates):
    with _lock:
        _save_json(TEMPLATES_FILE, templates)


def on_load():
    if not os.path.exists(CONFIG_FILE):
        _save_config(_default_config())
    if not os.path.exists(SCENES_FILE):
        _save_scenes([{
            'id': uuid.uuid4().hex[:10],
            'name': 'Desktop',
            'elements': [
                _clean_element({'source': 'desktop', 'x_pct': 0, 'y_pct': 0, 'w_pct': 100, 'h_pct': 100, 'border': False}),
                _clean_element({'source': 'camera', 'x_pct': 70, 'y_pct': 68, 'w_pct': 25, 'h_pct': 25}),
            ],
            'transition': 'crossfade',
            'transition_duration': DEFAULT_TRANSITION_DURATION,
        }])
    if not os.path.exists(TEMPLATES_FILE):
        _save_templates([])


# ── Broadcasting engine state ─────────────────────────────────────────
# Deliberately NEVER auto-started at boot (see module docstring / the
# WASAPI-safety precedent in audio_notes.py): opening a webcam or grabbing
# the screen is invasive (webcam light turns on, screen content is read)
# and belongs entirely to an explicit user click, never to on_load().
_state_lock = threading.Lock()
_active = False
_broadcast_thread = None
_stop_event = None
_start_error = None            # message from the last failed start, or None

_current_scene_id = None
_transition_lock = threading.Lock()
_transition = None             # {'from_frame','kind','duration','start'} or None
_last_composite = None         # last rendered frame (numpy array), used as transition source

_preview_lock = threading.Lock()
_latest_jpeg = None            # bytes, for the MJPEG preview stream

# Recording — video-only (no audio track; muxing mic/desktop audio into the
# file would need something like ffmpeg, which felt like overkill for what
# this module is). Only meaningful while broadcasting is active, since
# that's the only time composited frames exist to write at all. The actual
# cv2.VideoWriter is opened/closed exclusively from inside the broadcast
# loop thread (see _broadcast_loop) — _record_requested is just a flag the
# loop checks each frame, so no VideoWriter is ever touched from more than
# one thread.
_record_requested = False
_recording_active = False
_recording_path = None
_recording_started_at = None   # epoch seconds, for the frontend's elapsed timer


def _next_recording_path():
    base = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
    candidate = os.path.join(RECORDINGS_FOLDER, base + '.mp4')
    if not os.path.exists(candidate):
        return candidate
    version = 2
    while True:
        candidate = os.path.join(RECORDINGS_FOLDER, f'{base}_v{version}.mp4')
        if not os.path.exists(candidate):
            return candidate
        version += 1

_material_raw_cache = {}   # filename -> (mtime, ndarray) — unscaled; every box resizes it to its own size


def _material_path(filename):
    return os.path.join(MATERIALS_FOLDER, filename)


def _get_material_raw_frame(filename):
    import cv2
    path = _material_path(filename)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    cached = _material_raw_cache.get(filename)
    if cached and cached[0] == mtime:
        return cached[1]
    img = cv2.imread(path, cv2.IMREAD_COLOR)
    if img is None:
        return None
    _material_raw_cache[filename] = (mtime, img)
    return img


def _blank_frame(w, h):
    import numpy as np
    return np.zeros((h, w, 3), dtype='uint8')


def _capture_desktop_raw(sct, monitor_index):
    import cv2
    import numpy as np
    monitors = sct.monitors
    idx = monitor_index if 0 <= monitor_index < len(monitors) else 1
    raw = np.array(sct.grab(monitors[idx]))
    return cv2.cvtColor(raw, cv2.COLOR_BGRA2BGR)


# ── Single-window capture (pywin32) ───────────────────────────────────
# Grabbing the screen region a window occupies would show whatever's on
# top at that spot if something else overlaps it — PrintWindow is what
# actually asks the window to render its own content into our buffer,
# regardless of what's in front of it on screen.

def _find_window_by_title(title):
    import win32gui
    matches = []

    def handler(hwnd, _ctx):
        if win32gui.IsWindowVisible(hwnd) and win32gui.GetWindowText(hwnd) == title:
            matches.append(hwnd)
    win32gui.EnumWindows(handler, None)
    return matches[0] if matches else None


def _capture_window_raw(title):
    try:
        import win32gui
        import win32ui
        from ctypes import windll
        import numpy as np
        import cv2
    except ImportError:
        _log_error('window capture: pywin32 not installed')
        return None

    hwnd = _find_window_by_title(title)
    if not hwnd:
        return None
    hwnd_dc = mfc_dc = save_dc = bitmap = None
    try:
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        width, height = right - left, bottom - top
        if width <= 0 or height <= 0:
            return None

        hwnd_dc = win32gui.GetWindowDC(hwnd)
        mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
        save_dc = mfc_dc.CreateCompatibleDC()
        bitmap = win32ui.CreateBitmap()
        bitmap.CreateCompatibleBitmap(mfc_dc, width, height)
        save_dc.SelectObject(bitmap)

        # PW_RENDERFULLCONTENT (2) renders hardware-accelerated content
        # correctly on newer apps; fall back to the plain flag if it fails.
        result = windll.user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), 2)
        if not result:
            result = windll.user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), 0)
        if not result:
            return None

        info = bitmap.GetInfo()
        bits = bitmap.GetBitmapBits(True)
        img = np.frombuffer(bits, dtype='uint8').reshape((info['bmHeight'], info['bmWidth'], 4))
        return cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
    except Exception:
        _log_error('window capture')
        return None
    finally:
        try:
            if bitmap is not None:
                win32gui.DeleteObject(bitmap.GetHandle())
            if save_dc is not None:
                save_dc.DeleteDC()
            if mfc_dc is not None:
                mfc_dc.DeleteDC()
            if hwnd_dc is not None:
                win32gui.ReleaseDC(hwnd, hwnd_dc)
        except Exception:
            pass


def _render_elements(cam_frame, elements, w, h, sct, monitor_index):
    """Draws every positioned box onto a blank frame, in list order
    (later = on top). A desktop grab or a given window's capture is only
    ever taken once per frame, however many boxes reference it."""
    import cv2
    frame = _blank_frame(w, h)
    desktop_raw = None
    window_cache = {}

    for el in elements:
        x = int(el['x_pct'] / 100 * w)
        y = int(el['y_pct'] / 100 * h)
        ew = max(1, int(el['w_pct'] / 100 * w))
        eh = max(1, int(el['h_pct'] / 100 * h))

        source = el['source']
        if source == 'camera':
            src = cam_frame
        elif source == 'desktop':
            if desktop_raw is None:
                try:
                    desktop_raw = _capture_desktop_raw(sct, monitor_index)
                except Exception:
                    _log_error('desktop capture')
                    desktop_raw = _blank_frame(w, h)
            src = desktop_raw
        elif source.startswith(WINDOW_SOURCE_PREFIX):
            title = source[len(WINDOW_SOURCE_PREFIX):]
            if title not in window_cache:
                window_cache[title] = _capture_window_raw(title)
            src = window_cache[title]
        else:
            src = _get_material_raw_frame(source)
        if src is None:
            continue

        resized = cv2.resize(src, (ew, eh))
        x2, y2 = min(w, x + ew), min(h, y + eh)
        ew2, eh2 = x2 - x, y2 - y
        if ew2 <= 0 or eh2 <= 0:
            continue
        frame[y:y2, x:x2] = resized[:eh2, :ew2]
        if el.get('border', True):
            cv2.rectangle(frame, (x - 1, y - 1), (x2 + 1, y2 + 1), OVERLAY_BORDER_BGR, 2)
    return frame


def _blend(from_frame, to_frame, t, kind):
    import cv2
    import numpy as np
    if from_frame is None or from_frame.shape != to_frame.shape:
        return to_frame
    if kind == 'crossfade':
        return cv2.addWeighted(from_frame, 1 - t, to_frame, t, 0)
    if kind in ('slide-left', 'slide-right'):
        h, w = to_frame.shape[:2]
        offset = int(w * t)
        out = np.empty_like(to_frame)
        if kind == 'slide-left':
            out[:, :w - offset] = from_frame[:, offset:] if offset < w else from_frame[:, :0]
            out[:, w - offset:] = to_frame[:, :offset]
        else:
            out[:, offset:] = from_frame[:, :w - offset] if offset < w else from_frame[:, :0]
            out[:, :offset] = to_frame[:, w - offset:]
        return out
    return to_frame  # 'cut'


def _resolve_mic_inject_device(cfg):
    """Explicit config wins; otherwise auto-detect VB-Cable's input device
    by name (its default is "CABLE Input (VB-Audio Virtual Cable)")."""
    import sounddevice as sd
    if cfg.get('mic_inject_device_index') is not None:
        return cfg['mic_inject_device_index']
    try:
        for i, d in enumerate(sd.query_devices()):
            if 'CABLE Input' in d.get('name', '') and d.get('max_output_channels', 0) > 0:
                return i
    except Exception:
        pass
    return None


def _broadcast_loop(stop_event):
    global _active, _start_error, _current_scene_id, _last_composite, _latest_jpeg, _transition
    global _recording_active, _recording_path, _recording_started_at
    import cv2
    import mss

    cfg = _load_config()
    w, h, fps = cfg['output_width'], cfg['output_height'], cfg['output_fps']
    mirror = cfg.get('mirror_camera', True)

    cam = cv2.VideoCapture(cfg['camera_device_index'], cv2.CAP_DSHOW)
    sct = mss.mss()
    vcam = None
    recording_writer = None
    try:
        import pyvirtualcam
        backend = cfg.get('virtual_cam_backend', 'unitycapture')
        kwargs = {} if backend == 'auto' else {'backend': backend}
        vcam = pyvirtualcam.Camera(width=w, height=h, fps=fps, **kwargs)
    except Exception:
        _log_error('virtual camera open')
        with _state_lock:
            _start_error = ('Could not open the virtual camera. Make sure Unity Capture is installed, '
                             'and that no other app is already using it.')
            _active = False
        cam.release()
        return

    try:
        while not stop_event.is_set():
            ok, cam_frame = cam.read()
            if not ok:
                cam_frame = None
            elif mirror:
                cam_frame = cv2.flip(cam_frame, 1)

            scenes = _load_scenes()
            scene = _find_scene(scenes, _current_scene_id) or (scenes[0] if scenes else None)
            if scene is None:
                time_module.sleep(0.2)
                continue

            composite = _render_elements(cam_frame, scene.get('elements', []), w, h, sct, cfg['monitor_index'])

            with _transition_lock:
                tr = _transition
            if tr:
                elapsed = time_module.monotonic() - tr['start']
                t = min(1.0, elapsed / tr['duration']) if tr['duration'] > 0 else 1.0
                composite = _blend(tr['from_frame'], composite, t, tr['kind'])
                if t >= 1.0:
                    with _transition_lock:
                        _transition = None

            _last_composite = composite.copy()

            rgb = cv2.cvtColor(composite, cv2.COLOR_BGR2RGB)
            vcam.send(rgb)

            # Recording — opened/closed here, and only here, so the
            # VideoWriter never gets touched from more than this one thread.
            if _record_requested and recording_writer is None:
                try:
                    path = _next_recording_path()
                    fourcc = cv2.VideoWriter_fourcc(*'mp4v')
                    recording_writer = cv2.VideoWriter(path, fourcc, fps, (w, h))
                    _recording_path = path
                    _recording_started_at = time_module.time()
                    _recording_active = True
                except Exception:
                    _log_error('start recording')
                    recording_writer = None
            elif not _record_requested and recording_writer is not None:
                recording_writer.release()
                recording_writer = None
                _recording_active = False
                _recording_started_at = None
            if recording_writer is not None:
                recording_writer.write(composite)

            ok2, buf = cv2.imencode('.jpg', composite, [cv2.IMWRITE_JPEG_QUALITY, 70])
            if ok2:
                with _preview_lock:
                    _latest_jpeg = buf.tobytes()

            vcam.sleep_until_next_frame()
    except Exception:
        _log_error('broadcast loop')
    finally:
        cam.release()
        if vcam:
            vcam.close()
        if recording_writer is not None:
            recording_writer.release()
        with _state_lock:
            _active = False
            _recording_active = False
            _recording_started_at = None


def _start_broadcast():
    global _broadcast_thread, _stop_event, _active, _start_error, _current_scene_id, _record_requested
    with _state_lock:
        if _broadcast_thread and _broadcast_thread.is_alive():
            return
        scenes = _load_scenes()
        if not scenes:
            _start_error = 'No scenes are defined yet — add one first.'
            return
        if not _current_scene_id or not _find_scene(scenes, _current_scene_id):
            _current_scene_id = scenes[0]['id']
        _start_error = None
        _active = True
        _record_requested = False
        _stop_event = threading.Event()
        _broadcast_thread = threading.Thread(target=_broadcast_loop, args=(_stop_event,), daemon=True)
        _broadcast_thread.start()


def _stop_broadcast():
    global _broadcast_thread, _active, _record_requested
    with _state_lock:
        if _stop_event:
            _stop_event.set()
        _broadcast_thread = None
        _active = False
        _record_requested = False


# ── Sound board ────────────────────────────────────────────────────────
# Sound files must be WAV — read directly via Python's stdlib wave module
# (same choice audio_notes.py makes) so this module needs no extra
# dependency just to decode audio.

def _sound_path(filename):
    return os.path.join(SOUNDS_FOLDER, filename)


def _play_on_device(data, samplerate, channels, device_index):
    import sounddevice as sd
    try:
        # dtype must match the WAV's actual sample format (16-bit PCM) —
        # OutputStream defaults to float32, which raises a dtype mismatch
        # the moment real int16 samples are written to it.
        stream = sd.OutputStream(samplerate=samplerate, channels=channels, device=device_index, dtype='int16')
        stream.start()
        stream.write(data)
        stream.stop()
        stream.close()
    except Exception:
        _log_error(f'play sound on device {device_index}')


def _play_sound_file(filename):
    import wave
    import numpy as np
    path = _sound_path(filename)
    with wave.open(path, 'rb') as wf:
        channels = wf.getnchannels()
        rate = wf.getframerate()
        sampwidth = wf.getsampwidth()
        raw = wf.readframes(wf.getnframes())
    if sampwidth != 2:
        raise ValueError('Only 16-bit PCM WAV files are supported.')
    data = np.frombuffer(raw, dtype='int16')
    if channels > 1:
        data = data.reshape(-1, channels)

    cfg = _load_config()
    devices = [None]  # None = system default output (your speakers)
    mic_device = _resolve_mic_inject_device(cfg)
    if mic_device is not None:
        devices.append(mic_device)

    for dev in devices:
        threading.Thread(target=_play_on_device, args=(data, rate, channels, dev), daemon=True).start()


# ── Page ─────────────────────────────────────────────────────────────

@bp.route('/broadcaster')
def broadcaster_view():
    scenes = _load_scenes()
    materials = sorted(os.path.basename(p) for p in glob.glob(os.path.join(MATERIALS_FOLDER, '*')))
    sounds = sorted(os.path.basename(p) for p in glob.glob(os.path.join(SOUNDS_FOLDER, '*.wav')))
    return render_template(
        'broadcaster.html',
        scenes=scenes,
        materials=materials,
        sounds=sounds,
        templates=_load_templates(),
        config=_load_config(),
        transitions=TRANSITIONS,
    )


# ── Live preview (MJPEG) ────────────────────────────────────────────
_PLACEHOLDER_JPEG = None


def _placeholder_jpeg():
    global _PLACEHOLDER_JPEG
    if _PLACEHOLDER_JPEG is None:
        import cv2
        frame = _blank_frame(480, 270)
        cv2.putText(frame, 'Broadcast not active', (110, 140), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (100, 100, 100), 2)
        ok, buf = cv2.imencode('.jpg', frame)
        _PLACEHOLDER_JPEG = buf.tobytes() if ok else b''
    return _PLACEHOLDER_JPEG


@bp.route('/broadcaster/preview.mjpg')
def preview_stream():
    def gen():
        while True:
            with _state_lock:
                active = _active
            if active:
                with _preview_lock:
                    frame = _latest_jpeg
                frame = frame or _placeholder_jpeg()
            else:
                frame = _placeholder_jpeg()
            yield (b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + frame + b'\r\n')
            time_module.sleep(0.1)  # ~10fps preview, independent of the real output fps
    return Response(gen(), mimetype='multipart/x-mixed-replace; boundary=frame')


# ── API: broadcast lifecycle ─────────────────────────────────────────

@bp.route('/api/broadcaster/toggle', methods=['POST'])
def toggle_route():
    with _state_lock:
        currently_active = _active
    if currently_active:
        _stop_broadcast()
    else:
        _start_broadcast()
        time_module.sleep(0.3)  # give the thread a moment to fail fast if a device can't open
    with _state_lock:
        return jsonify({'status': 'ok', 'active': _active, 'error': _start_error})


@bp.route('/api/broadcaster/status')
def status_route():
    with _state_lock:
        active, error = _active, _start_error
    with _transition_lock:
        transitioning = _transition is not None
    return jsonify({
        'active': active,
        'error': error,
        'current_scene_id': _current_scene_id,
        'transitioning': transitioning,
        'recording_active': _recording_active,
        'recording_started_at': (int(_recording_started_at * 1000) if _recording_active and _recording_started_at else None),
        'recording_file': (os.path.basename(_recording_path) if _recording_active and _recording_path else None),
    })


@bp.route('/api/broadcaster/record/toggle', methods=['POST'])
def toggle_record_route():
    global _record_requested
    with _state_lock:
        if not _active:
            return jsonify({'status': 'error', 'message': 'Start broadcasting first.'}), 400
        _record_requested = not _record_requested
        requested = _record_requested
    return jsonify({'status': 'ok', 'requested': requested})


# ── API: scenes ──────────────────────────────────────────────────────

def _scene_from_payload(data, existing=None):
    scene = dict(existing) if existing else {'id': uuid.uuid4().hex[:10]}
    if 'name' in data:
        scene['name'] = (data.get('name') or '').strip() or scene.get('name', 'Scene')
    elif 'name' not in scene:
        scene['name'] = 'Scene'
    if 'elements' in data:
        scene['elements'] = [_clean_element(e) for e in (data.get('elements') or [])]
    elif 'elements' not in scene:
        scene['elements'] = []
    if 'transition' in data and data['transition'] in TRANSITIONS:
        scene['transition'] = data['transition']
    elif 'transition' not in scene:
        scene['transition'] = 'crossfade'
    if 'transition_duration' in data:
        scene['transition_duration'] = float(data['transition_duration'])
    elif 'transition_duration' not in scene:
        scene['transition_duration'] = DEFAULT_TRANSITION_DURATION
    return scene


@bp.route('/api/broadcaster/scenes', methods=['GET'])
def list_scenes_route():
    return jsonify(_load_scenes())


@bp.route('/api/broadcaster/scenes', methods=['POST'])
def create_scene_route():
    scene = _scene_from_payload(request.json or {})
    scenes = _load_scenes()
    scenes.append(scene)
    _save_scenes(scenes)
    return jsonify({'status': 'ok', 'scene': scene})


@bp.route('/api/broadcaster/scenes/<sid>', methods=['POST'])
def update_scene_route(sid):
    scenes = _load_scenes()
    existing = _find_scene(scenes, sid)
    if not existing:
        return jsonify({'status': 'error', 'message': 'Scene not found.'}), 404
    updated = _scene_from_payload(request.json or {}, existing=existing)
    scenes = [updated if s['id'] == sid else s for s in scenes]
    _save_scenes(scenes)
    return jsonify({'status': 'ok', 'scene': updated})


@bp.route('/api/broadcaster/scenes/<sid>/delete', methods=['POST'])
def delete_scene_route(sid):
    scenes = [s for s in _load_scenes() if s['id'] != sid]
    _save_scenes(scenes)
    return jsonify({'status': 'ok'})


@bp.route('/api/broadcaster/scenes/<sid>/switch', methods=['POST'])
def switch_scene_route(sid):
    global _current_scene_id, _transition
    scenes = _load_scenes()
    scene = _find_scene(scenes, sid)
    if not scene:
        return jsonify({'status': 'error', 'message': 'Scene not found.'}), 404

    with _state_lock:
        active = _active
    if active:
        with _transition_lock:
            _transition = {
                'from_frame': _last_composite.copy() if _last_composite is not None else None,
                'kind': scene.get('transition', 'crossfade'),
                'duration': scene.get('transition_duration', DEFAULT_TRANSITION_DURATION),
                'start': time_module.monotonic(),
            }
    _current_scene_id = sid
    return jsonify({'status': 'ok'})


# ── API: templates (saved box layouts) ────────────────────────────────

@bp.route('/api/broadcaster/templates', methods=['GET'])
def list_templates_route():
    return jsonify(_load_templates())


@bp.route('/api/broadcaster/templates', methods=['POST'])
def create_template_route():
    data = request.json or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'status': 'error', 'message': 'A name is required.'}), 400
    template = {
        'id': uuid.uuid4().hex[:10],
        'name': name,
        'elements': [_clean_element(e) for e in (data.get('elements') or [])],
    }
    templates = _load_templates()
    templates.append(template)
    _save_templates(templates)
    return jsonify({'status': 'ok', 'template': template})


@bp.route('/api/broadcaster/templates/<tid>/delete', methods=['POST'])
def delete_template_route(tid):
    templates = [t for t in _load_templates() if t['id'] != tid]
    _save_templates(templates)
    return jsonify({'status': 'ok'})


# ── API: materials ───────────────────────────────────────────────────

@bp.route('/api/broadcaster/materials', methods=['GET'])
def list_materials_route():
    return jsonify(sorted(os.path.basename(p) for p in glob.glob(os.path.join(MATERIALS_FOLDER, '*'))))


@bp.route('/api/broadcaster/materials/upload', methods=['POST'])
def upload_material_route():
    f = request.files.get('file')
    if f is None or not f.filename:
        return jsonify({'status': 'error', 'message': 'No file received.'}), 400
    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in ('.png', '.jpg', '.jpeg', '.bmp', '.webp'):
        return jsonify({'status': 'error', 'message': 'Use an image file (png/jpg/bmp/webp).'}), 400
    safe_name = re.sub(r'[^\w\-. ]', '_', os.path.splitext(f.filename)[0]) + ext
    f.save(_material_path(safe_name))
    return jsonify({'status': 'ok', 'filename': safe_name})


@bp.route('/api/broadcaster/materials/<name>/delete', methods=['POST'])
def delete_material_route(name):
    path = _material_path(name)
    if os.path.exists(path):
        os.remove(path)
    _material_raw_cache.pop(name, None)
    return jsonify({'status': 'ok'})


@bp.route('/broadcaster/materials/<path:name>')
def serve_material_route(name):
    return send_from_directory(MATERIALS_FOLDER, name)


# ── API: sounds ──────────────────────────────────────────────────────

@bp.route('/api/broadcaster/sounds', methods=['GET'])
def list_sounds_route():
    return jsonify(sorted(os.path.basename(p) for p in glob.glob(os.path.join(SOUNDS_FOLDER, '*.wav'))))


@bp.route('/api/broadcaster/sounds/upload', methods=['POST'])
def upload_sound_route():
    f = request.files.get('file')
    if f is None or not f.filename:
        return jsonify({'status': 'error', 'message': 'No file received.'}), 400
    if os.path.splitext(f.filename)[1].lower() != '.wav':
        return jsonify({'status': 'error', 'message': 'Only .wav files are supported.'}), 400
    safe_name = re.sub(r'[^\w\-. ]', '_', os.path.splitext(f.filename)[0]) + '.wav'
    f.save(_sound_path(safe_name))
    return jsonify({'status': 'ok', 'filename': safe_name})


@bp.route('/api/broadcaster/sounds/<name>/delete', methods=['POST'])
def delete_sound_route(name):
    path = _sound_path(name)
    if os.path.exists(path):
        os.remove(path)
    return jsonify({'status': 'ok'})


@bp.route('/api/broadcaster/sounds/<name>/play', methods=['POST'])
def play_sound_route(name):
    path = _sound_path(name)
    if not os.path.exists(path):
        return jsonify({'status': 'error', 'message': 'Sound not found.'}), 404
    try:
        _play_sound_file(name)
    except Exception as e:
        _log_error('play sound')
        return jsonify({'status': 'error', 'message': f'Could not play that sound: {e}'}), 500
    return jsonify({'status': 'ok'})


# ── API: devices / windows + config (simplified Setup section) ───────

@bp.route('/api/broadcaster/devices')
def devices_route():
    cameras = []
    try:
        import cv2
        for i in range(5):
            cap = cv2.VideoCapture(i, cv2.CAP_DSHOW)
            if cap.isOpened():
                cameras.append({'index': i, 'label': f'Camera {i}'})
            cap.release()
    except Exception:
        _log_error('enumerate cameras')

    monitors = []
    try:
        import mss
        with mss.mss() as sct:
            for i, m in enumerate(sct.monitors):
                label = 'All monitors combined' if i == 0 else f'Monitor {i} ({m["width"]}x{m["height"]})'
                monitors.append({'index': i, 'label': label})
    except Exception:
        _log_error('enumerate monitors')

    audio_outputs = []
    try:
        import sounddevice as sd
        for i, d in enumerate(sd.query_devices()):
            if d.get('max_output_channels', 0) > 0:
                audio_outputs.append({'index': i, 'label': d['name']})
    except Exception:
        _log_error('enumerate audio devices')

    return jsonify({'cameras': cameras, 'monitors': monitors, 'audio_outputs': audio_outputs})


@bp.route('/api/broadcaster/windows')
def windows_route():
    """Currently open, visible top-level windows by title — e.g. so a box
    can be pointed at just "Book1.xlsx - Excel" instead of the whole
    desktop. Fetched fresh each time (no caching) since what's open
    changes constantly."""
    titles = []
    try:
        import win32gui

        def handler(hwnd, _ctx):
            if win32gui.IsWindowVisible(hwnd):
                title = win32gui.GetWindowText(hwnd).strip()
                if title:
                    titles.append(title)
        win32gui.EnumWindows(handler, None)
    except Exception:
        _log_error('enumerate windows')

    seen = set()
    unique = []
    for t in titles:
        if t not in seen:
            seen.add(t)
            unique.append(t)
    return jsonify(sorted(unique, key=str.lower))


@bp.route('/api/broadcaster/config', methods=['GET'])
def get_config_route():
    return jsonify(_load_config())


@bp.route('/api/broadcaster/config', methods=['POST'])
def save_config_route():
    data = request.json or {}
    cfg = _load_config()

    # The simplified Setup UI sends a single "quality" preset key instead
    # of width/height/fps individually — expand it here.
    quality = data.get('quality')
    if quality in QUALITY_PRESETS:
        cfg['output_width'], cfg['output_height'], cfg['output_fps'] = QUALITY_PRESETS[quality]

    for key in ('camera_device_index', 'monitor_index', 'mic_inject_device_index'):
        if key in data:
            cfg[key] = data[key]
    if 'mirror_camera' in data:
        cfg['mirror_camera'] = bool(data['mirror_camera'])

    _save_config(cfg)
    return jsonify({'status': 'ok', 'config': cfg})
