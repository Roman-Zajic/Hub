"""
Broadcaster module — a small "presenter" tool. A scene is simply an ordered
list of positioned "elements" (boxes) — each pointing at the camera, the
desktop, a specific open window (e.g. just Excel or PowerPoint instead of
the whole screen), or an uploaded material image. There's no separate
"background" concept: stretch a box to cover the whole frame and it acts
as one; leave it small and it's picture-in-picture. Later elements in the
list draw on top of earlier ones.

── Output model ───────────────────────────────────────────────────────
This module does NOT feed a virtual camera. Teams (and most video call
apps) re-encode camera input aggressively, which makes fine text in a
shared document/spreadsheet box unreadable. Instead, the composited frame
is rendered into a plain, chrome-less browser window — /broadcaster/output
— that you open once and then share as a WINDOW (or the whole desktop) via
your call app's "Share Content" feature. Window/desktop capture goes
through a much lighter encode path than a virtual camera device, so text
stays sharp.

Because you're now sharing your screen (not looking at this app's UI while
presenting), scene switching and sound-board playback are driven by
SYSTEM-WIDE keyboard hotkeys (via the "keyboard" package) instead of
clicking buttons in this page — they fire no matter which window/app has
focus. See SCENE_HOTKEY_POOL / SOUND_HOTKEY_POOL below for the default
assignments; each scene/sound can be individually rebound from the UI.

── Hardware/driver requirements ──────────────────────────────────────
  pip install opencv-python mss sounddevice numpy pywin32 keyboard --break-system-packages

  - Mic passthrough / sound board injection still needs "VB-Cable"
    (https://vb-audio.com/Cable/) installed once. Set "CABLE Output
    (VB-Audio Virtual Cable)" as your microphone in Teams/etc — do this
    ONCE; you never touch Teams' device selection again after that. See
    the "Mic Passthrough" docstring block further down for how this works.
  - Single-window capture: grabbing just the screen region a window
    occupies (the naive approach) would show whatever's actually on top
    at that spot if another window overlaps it — not what "just Excel"
    means. Real per-window capture needs Windows' own PrintWindow API,
    which is what pywin32 provides.
  - System-wide hotkeys: the "keyboard" package installs a low-level
    Windows keyboard hook. If the hotkey doesn't fire while some other
    app is focused, that app is very likely running elevated (as
    Administrator) — Windows won't deliver hook events from a
    non-elevated process to an elevated foreground window. Run this
    app's process as Administrator too if that happens.

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

# JPEG quality for the output-window / preview stream. No longer
# constrained by a virtual-cam's frame budget, so this is pushed up from
# the old preview-thumbnail quality (70) for crisper shared text.
OUTPUT_JPEG_QUALITY = 90

TRANSITIONS = ('cut', 'crossfade', 'slide-left', 'slide-right')

# Quality presets shown in the simplified Setup UI — (width, height, fps).
# 'ultra' is the one to reach for when presenting documents/spreadsheets:
# fine text needs real resolution, and a lower fps there is a worthwhile
# trade (nobody needs 30fps of a static spreadsheet).
QUALITY_PRESETS = {
    'low': (640, 360, 24),
    'balanced': (1280, 720, 30),
    'high': (1920, 1080, 30),
    'ultra': (2560, 1440, 24),
}

# ── Hotkeys ────────────────────────────────────────────────────────────
# Default pools assigned in creation order — scenes and sounds live in
# separate modifier spaces so they can never collide with each other by
# default. Either can be individually rebound from the UI at any time;
# rebinding just needs to avoid whatever's already used across BOTH pools
# (see _all_used_hotkeys).
SCENE_HOTKEY_POOL = [f'ctrl+alt+{d}' for d in '1234567890']
SOUND_HOTKEY_POOL = [f'ctrl+alt+shift+{d}' for d in '1234567890']

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
SOUND_HOTKEYS_FILE = os.path.join(DATA_FOLDER, 'sound_hotkeys.json')
ERROR_LOG = os.path.join(DATA_FOLDER, 'broadcaster_error.log')

_lock = threading.Lock()   # guards config/scenes/templates/sound_hotkeys read-modify-write on disk


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
        'output_width': DEFAULT_OUTPUT_WIDTH,
        'output_height': DEFAULT_OUTPUT_HEIGHT,
        'output_fps': DEFAULT_OUTPUT_FPS,
        'mic_inject_device_index': None,          # None = auto-detect "CABLE Input" — the VB-Cable side Teams listens to
        'mic_passthrough_source_device_index': None,  # None = OS default input (your real physical mic)
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
    'elements' list plus a 'hotkey' field) — see the module history for
    the previous shapes this converts from. Nothing saved before this
    change is lost — it's just re-expressed as boxes, and any scene
    missing a 'hotkey' key just gets None (assigned a default later by
    _assign_default_hotkeys)."""
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

    # Legacy config.json's virtual_cam_backend key, if present, is simply
    # ignored now — the virtual camera output path was removed entirely.
    s.pop('virtual_cam_backend', None)

    s['elements'] = [_clean_element(e) for e in elements]
    s.setdefault('hotkey', None)
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


def _load_sound_hotkeys():
    return _load_json(SOUND_HOTKEYS_FILE, {})


def _save_sound_hotkeys(data):
    with _lock:
        _save_json(SOUND_HOTKEYS_FILE, data)


# ── Hotkey assignment/registration ────────────────────────────────────

def _used_scene_hotkeys(scenes, except_id=None):
    return {s['hotkey'] for s in scenes if s.get('hotkey') and s['id'] != except_id}


def _used_sound_hotkeys(sound_hotkeys, except_name=None):
    return {hk for name, hk in sound_hotkeys.items() if hk and name != except_name}


def _all_used_hotkeys(except_scene=None, except_sound=None):
    scenes = _load_scenes()
    sound_hk = _load_sound_hotkeys()
    return _used_scene_hotkeys(scenes, except_scene) | _used_sound_hotkeys(sound_hk, except_sound)


def _next_free_hotkey(pool, used):
    for hk in pool:
        if hk not in used:
            return hk
    return None  # pool exhausted — caller just leaves it unassigned


def _assign_default_hotkeys():
    """Fills in a default hotkey for any scene/sound that doesn't have one
    yet — called once at startup and after any scene/sound is created."""
    scenes = _load_scenes()
    sound_hk = _load_sound_hotkeys()
    used = _used_scene_hotkeys(scenes) | _used_sound_hotkeys(sound_hk)

    changed_scenes = False
    for s in scenes:
        if not s.get('hotkey'):
            hk = _next_free_hotkey(SCENE_HOTKEY_POOL, used)
            if hk:
                s['hotkey'] = hk
                used.add(hk)
                changed_scenes = True
    if changed_scenes:
        _save_scenes(scenes)

    sound_files = sorted(os.path.basename(p) for p in glob.glob(os.path.join(SOUNDS_FOLDER, '*.wav')))
    changed_sounds = False
    for name in sound_files:
        if not sound_hk.get(name):
            hk = _next_free_hotkey(SOUND_HOTKEY_POOL, used)
            if hk:
                sound_hk[name] = hk
                used.add(hk)
                changed_sounds = True
    if changed_sounds:
        _save_sound_hotkeys(sound_hk)


_HOTKEY_LIB_ERROR = None


def _get_keyboard_lib():
    global _HOTKEY_LIB_ERROR
    try:
        import keyboard
        _HOTKEY_LIB_ERROR = None
        return keyboard
    except ImportError:
        _HOTKEY_LIB_ERROR = ('The "keyboard" package is not installed — system-wide scene/sound '
                              'hotkeys are disabled. Run: pip install keyboard --break-system-packages '
                              '(then restart the app).')
        return None


_hotkey_lock = threading.Lock()

_hotkey_diag_lock = threading.Lock()
_hotkeys_registered_count = 0
_last_hotkey_fired = None  # {'hotkey': str, 'kind': 'scene'|'sound', 'target': str, 'at': epoch seconds} or None


def _switch_scene(sid):
    """Shared by the manual Switch button and by a scene hotkey firing."""
    global _current_scene_id, _transition
    scenes = _load_scenes()
    scene = _find_scene(scenes, sid)
    if not scene:
        return False
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
    return True


def _hotkey_switch_scene(sid):
    global _last_hotkey_fired
    try:
        scenes = _load_scenes()
        scene = _find_scene(scenes, sid)
        with _hotkey_diag_lock:
            _last_hotkey_fired = {
                'hotkey': (scene or {}).get('hotkey'),
                'kind': 'scene',
                'target': (scene or {}).get('name', sid),
                'at': time_module.time(),
            }
        _switch_scene(sid)
    except Exception:
        _log_error('hotkey switch scene')


def _hotkey_play_sound(filename):
    global _last_hotkey_fired
    try:
        sound_hk = _load_sound_hotkeys()
        with _hotkey_diag_lock:
            _last_hotkey_fired = {
                'hotkey': sound_hk.get(filename),
                'kind': 'sound',
                'target': filename,
                'at': time_module.time(),
            }
        if os.path.exists(_sound_path(filename)):
            _play_sound_file(filename)
    except Exception:
        _log_error('hotkey play sound')


def _refresh_hotkeys():
    """Tears down and re-registers every scene/sound hotkey against the
    keyboard package's global hook. Called after any hotkey/scene/sound
    change — cheap and rare enough that a full unhook+rehook each time is
    simpler than diffing what changed."""
    global _hotkeys_registered_count
    kb = _get_keyboard_lib()
    if kb is None:
        with _hotkey_diag_lock:
            _hotkeys_registered_count = 0
        return
    with _hotkey_lock:
        try:
            kb.unhook_all_hotkeys()
        except Exception:
            # keyboard's unhook_all_hotkeys() raises AttributeError if no
            # hotkey has ever been registered yet in this process — its
            # internal listener lazily creates the tracking attribute on
            # the first add_hotkey() call, so this always fires on the
            # very first refresh after startup. Harmless: just log it and
            # fall through to registration instead of aborting the whole
            # refresh (which previously left every hotkey unregistered).
            _log_error('unhook hotkeys (non-fatal — nothing registered yet)')
        count = 0
        for s in _load_scenes():
            hk = s.get('hotkey')
            if hk:
                try:
                    kb.add_hotkey(hk, _hotkey_switch_scene, args=(s['id'],))
                    count += 1
                except Exception:
                    _log_error(f'register scene hotkey "{hk}"')
        for filename, hk in _load_sound_hotkeys().items():
            if hk:
                try:
                    kb.add_hotkey(hk, _hotkey_play_sound, args=(filename,))
                    count += 1
                except Exception:
                    _log_error(f'register sound hotkey "{hk}"')
        with _hotkey_diag_lock:
            _hotkeys_registered_count = count


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
            'hotkey': None,
        }])
    if not os.path.exists(TEMPLATES_FILE):
        _save_templates([])
    _assign_default_hotkeys()
    _refresh_hotkeys()


# ── Presentation engine state ─────────────────────────────────────────
# Deliberately NEVER auto-started at boot: opening a webcam or grabbing
# the screen is invasive (webcam light turns on, screen content is read)
# and belongs entirely to an explicit user click, never to on_load(). The
# mic passthrough stream below follows the same rule for the same reason
# (opening an audio device is likewise invasive). Hotkey REGISTRATION
# (above) is the exception — that's just listening for key combos, not
# touching any camera/screen/audio device, so it's safe at boot.
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
_latest_jpeg = None            # bytes — feeds BOTH the small in-page preview and the output window

# Recording — video-only (no audio track; muxing mic/desktop audio into the
# file would need something like ffmpeg, which felt like overkill for what
# this module is). Only meaningful while presenting is active, since
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


# ── Resize quality ───────────────────────────────────────────────────
# cv2.resize()'s default interpolation (INTER_LINEAR) blurs fine detail —
# most noticeable as unreadable document/spreadsheet text once a box is
# scaled to the output frame. INTER_AREA is the correct choice when
# shrinking (it area-averages instead of blurring), and INTER_LANCZOS4
# is sharper than linear when enlarging.
def _smart_resize(src, target_w, target_h):
    import cv2
    src_h, src_w = src.shape[:2]
    if target_w == src_w and target_h == src_h:
        return src
    shrinking = (target_w * target_h) < (src_w * src_h)
    interp = cv2.INTER_AREA if shrinking else cv2.INTER_LANCZOS4
    return cv2.resize(src, (target_w, target_h), interpolation=interp)


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

        resized = _smart_resize(src, ew, eh)
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
    by name (its default is "CABLE Input (VB-Audio Virtual Cable)") — this
    is the device Teams listens to, and the one both one-shot sound-effect
    injection (legacy, non-passthrough path) and the continuous mic
    passthrough stream write into."""
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
    """Renders composited frames at the configured fps and encodes each one
    to JPEG for the preview/output stream (and, if requested, to the
    recording file). No virtual camera involved — the output window
    (/broadcaster/output) IS the "device"; sharing that window/desktop in
    your call app is what gets the frame to the other side."""
    global _active, _current_scene_id, _last_composite, _latest_jpeg
    global _recording_active, _recording_path, _recording_started_at
    global _transition
    import cv2
    import mss

    cfg = _load_config()
    w, h, fps = cfg['output_width'], cfg['output_height'], cfg['output_fps']
    mirror = cfg.get('mirror_camera', True)
    frame_interval = 1.0 / fps if fps > 0 else 1.0 / 30

    cam = cv2.VideoCapture(cfg['camera_device_index'], cv2.CAP_DSHOW)
    sct = mss.mss()
    recording_writer = None

    try:
        while not stop_event.is_set():
            frame_start = time_module.monotonic()

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

            ok2, buf = cv2.imencode('.jpg', composite, [cv2.IMWRITE_JPEG_QUALITY, OUTPUT_JPEG_QUALITY])
            if ok2:
                with _preview_lock:
                    _latest_jpeg = buf.tobytes()

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

            elapsed_frame = time_module.monotonic() - frame_start
            sleep_left = frame_interval - elapsed_frame
            if sleep_left > 0:
                time_module.sleep(sleep_left)
    except Exception:
        _log_error('broadcast loop')
    finally:
        cam.release()
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


def _resample_linear_mono(samples, orig_sr, target_sr):
    """Simple linear-interpolation resampler, mono float32 in [-1, 1] —
    same technique used in audio_notes.py's _resample_linear, kept as a
    separate small copy here so this module has no cross-module import
    dependency on audio_notes.py (which may not even be installed)."""
    import numpy as np
    if orig_sr == target_sr or len(samples) < 2:
        return samples.astype('float32')
    duration = len(samples) / orig_sr
    target_len = max(1, int(round(duration * target_sr)))
    orig_idx = np.linspace(0, len(samples) - 1, num=len(samples))
    target_idx = np.linspace(0, len(samples) - 1, num=target_len)
    return np.interp(target_idx, orig_idx, samples).astype('float32')


# ── Mic Passthrough ──────────────────────────────────────────────────
# Windows has no software mixing point on a *physical* microphone — only
# a virtual audio device (VB-Cable) can be written to by Python and read
# by Teams as an input. So instead of the sound board briefly borrowing
# whatever device Teams is currently listening to (the old one-shot
# behavior, still used as a fallback below when passthrough is off), Mic
# Passthrough runs a continuous full-duplex stream for as long as it's
# toggled on: your real physical mic in one ear, VB-Cable's input in the
# other, mixed together sample-by-sample, with sound-board clips layered
# on top as they're triggered. Practical effect: set Teams' microphone to
# "CABLE Output (VB-Audio Virtual Cable)" ONCE in Teams' own settings,
# and never touch that dropdown again — muting/unmuting in Teams still
# works exactly as before (it just stops sending from that one device),
# and playing a sound board clip no longer requires switching anything.
PASSTHROUGH_SAMPLERATE = 48000
PASSTHROUGH_BLOCKSIZE = 480   # 10ms @ 48kHz — small enough to feel live, large enough to avoid glitching

_passthrough_state_lock = threading.Lock()
_passthrough_stream = None
_passthrough_active = False
_passthrough_error = None

_clips_lock = threading.Lock()
_active_clips = []   # [{'data': float32 ndarray mono @ PASSTHROUGH_SAMPLERATE, 'pos': int}, ...]


def _passthrough_callback(indata, outdata, frames, time_info, status):
    # status flags (e.g. input/output underflow) are common on a heavily
    # loaded machine and not worth spamming the error log on every tick —
    # they're transient audio glitches, not something actionable here.
    mic = indata[:, 0] if indata.shape[1] else None
    import numpy as np
    mixed = mic.astype('float32').copy() if mic is not None else np.zeros(frames, dtype='float32')

    with _clips_lock:
        finished = []
        for i, clip in enumerate(_active_clips):
            data, pos = clip['data'], clip['pos']
            take = min(len(data) - pos, frames)
            if take > 0:
                mixed[:take] += data[pos:pos + take]
            clip['pos'] += take
            if clip['pos'] >= len(data):
                finished.append(i)
        for i in reversed(finished):
            _active_clips.pop(i)

    np.clip(mixed, -1.0, 1.0, out=mixed)
    for ch in range(outdata.shape[1]):
        outdata[:, ch] = mixed


def _start_mic_passthrough():
    global _passthrough_stream, _passthrough_active, _passthrough_error
    with _passthrough_state_lock:
        if _passthrough_stream is not None:
            return
        import sounddevice as sd
        cfg = _load_config()
        mic_src = cfg.get('mic_passthrough_source_device_index')   # None = OS default input
        cable_out = _resolve_mic_inject_device(cfg)
        if cable_out is None:
            _passthrough_error = 'No VB-Cable input device found — set "Mic-injection device" in Setup.'
            raise RuntimeError(_passthrough_error)
        try:
            stream = sd.Stream(
                device=(mic_src, cable_out),
                samplerate=PASSTHROUGH_SAMPLERATE,
                blocksize=PASSTHROUGH_BLOCKSIZE,
                channels=1,
                dtype='float32',
                callback=_passthrough_callback,
            )
            stream.start()
        except Exception as e:
            _log_error('mic passthrough start')
            _passthrough_error = (f'Could not open the mic passthrough stream: {e}. Check the physical '
                                   f'microphone and Mic-injection (VB-Cable) device selections in Setup.')
            raise RuntimeError(_passthrough_error)
        _passthrough_stream = stream
        _passthrough_active = True
        _passthrough_error = None


def _stop_mic_passthrough():
    global _passthrough_stream, _passthrough_active
    with _passthrough_state_lock:
        if _passthrough_stream is not None:
            try:
                _passthrough_stream.stop()
                _passthrough_stream.close()
            except Exception:
                _log_error('mic passthrough stop')
            _passthrough_stream = None
        _passthrough_active = False
        with _clips_lock:
            _active_clips.clear()


def _inject_clip_into_passthrough(data, rate, channels):
    """Converts a just-triggered sound-board clip to mono float32 at
    PASSTHROUGH_SAMPLERATE and queues it to be mixed into the next
    _passthrough_callback ticks, alongside the live mic signal."""
    import numpy as np
    if channels > 1:
        mono_i16 = data.reshape(-1, channels).mean(axis=1)
    else:
        mono_i16 = data.astype('float32')
    mono = (mono_i16.astype('float32') / 32768.0)
    resampled = _resample_linear_mono(mono, rate, PASSTHROUGH_SAMPLERATE)
    with _clips_lock:
        _active_clips.append({'data': resampled, 'pos': 0})


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

    # Always play locally so the presenter can hear it too.
    threading.Thread(target=_play_on_device, args=(data, rate, channels, None), daemon=True).start()

    with _passthrough_state_lock:
        passthrough_on = _passthrough_active

    if passthrough_on:
        # Continuous passthrough already owns the VB-Cable device — mix
        # the clip into that same stream rather than opening a second,
        # competing connection to it.
        _inject_clip_into_passthrough(data, rate, channels)
    else:
        # Legacy one-shot behavior: briefly write straight to whichever
        # device is configured as the mic-inject target.
        cfg = _load_config()
        mic_device = _resolve_mic_inject_device(cfg)
        if mic_device is not None:
            threading.Thread(target=_play_on_device, args=(data, rate, channels, mic_device), daemon=True).start()


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
        sound_hotkeys=_load_sound_hotkeys(),
        templates=_load_templates(),
        config=_load_config(),
        transitions=TRANSITIONS,
    )


@bp.route('/broadcaster/output')
def output_view():
    """Clean, chrome-less full-bleed view of the composited output —
    open this once and share THIS WINDOW (not the whole desktop, though
    that works too) in your call app. Deliberately does not extend
    layout.html — no sidebar/navbar, just the image, so there's nothing
    in the shared window except the presentation itself."""
    return render_template('broadcaster_output.html')


# ── Live preview / output stream (MJPEG) ────────────────────────────
_PLACEHOLDER_JPEG = None


def _placeholder_jpeg():
    global _PLACEHOLDER_JPEG
    if _PLACEHOLDER_JPEG is None:
        import cv2
        frame = _blank_frame(480, 270)
        cv2.putText(frame, 'Presentation not started', (80, 140), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (100, 100, 100), 2)
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
            time_module.sleep(0.1)  # ~10fps refresh for the stream itself; the real fps is the loop's own pacing
    return Response(gen(), mimetype='multipart/x-mixed-replace; boundary=frame')


# ── API: presentation lifecycle ──────────────────────────────────────

@bp.route('/api/broadcaster/toggle', methods=['POST'])
def toggle_route():
    with _state_lock:
        currently_active = _active
    if currently_active:
        _stop_broadcast()
    else:
        _start_broadcast()
    with _state_lock:
        return jsonify({'status': 'ok', 'active': _active, 'error': _start_error})


@bp.route('/api/broadcaster/status')
def status_route():
    with _state_lock:
        active, error = _active, _start_error
    with _transition_lock:
        transitioning = _transition is not None
    with _passthrough_state_lock:
        passthrough_active, passthrough_error = _passthrough_active, _passthrough_error
    with _hotkey_diag_lock:
        hk_count = _hotkeys_registered_count
        last_fired = dict(_last_hotkey_fired) if _last_hotkey_fired else None
    return jsonify({
        'active': active,
        'error': error,
        'current_scene_id': _current_scene_id,
        'transitioning': transitioning,
        'recording_active': _recording_active,
        'recording_started_at': (int(_recording_started_at * 1000) if _recording_active and _recording_started_at else None),
        'recording_file': (os.path.basename(_recording_path) if _recording_active and _recording_path else None),
        'passthrough_active': passthrough_active,
        'passthrough_error': passthrough_error,
        'hotkeys_error': _HOTKEY_LIB_ERROR,
        'hotkeys_registered_count': hk_count,
        'last_hotkey_fired': last_fired,
    })


@bp.route('/api/broadcaster/record/toggle', methods=['POST'])
def toggle_record_route():
    global _record_requested
    with _state_lock:
        if not _active:
            return jsonify({'status': 'error', 'message': 'Start the presentation first.'}), 400
        _record_requested = not _record_requested
        requested = _record_requested
    return jsonify({'status': 'ok', 'requested': requested})


# ── API: mic passthrough lifecycle ───────────────────────────────────

@bp.route('/api/broadcaster/passthrough/toggle', methods=['POST'])
def toggle_passthrough_route():
    with _passthrough_state_lock:
        currently_on = _passthrough_active
    try:
        if currently_on:
            _stop_mic_passthrough()
        else:
            _start_mic_passthrough()
    except Exception as e:
        with _passthrough_state_lock:
            return jsonify({'status': 'error', 'active': _passthrough_active, 'message': str(e)})
    with _passthrough_state_lock:
        return jsonify({'status': 'ok', 'active': _passthrough_active})


# ── API: scenes ──────────────────────────────────────────────────────

def _scene_from_payload(data, existing=None):
    scene = dict(existing) if existing else {'id': uuid.uuid4().hex[:10], 'hotkey': None}
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
    scene.setdefault('hotkey', None)
    return scene


@bp.route('/api/broadcaster/scenes', methods=['GET'])
def list_scenes_route():
    return jsonify(_load_scenes())


@bp.route('/api/broadcaster/scenes', methods=['POST'])
def create_scene_route():
    scene = _scene_from_payload(request.json or {})
    if not scene.get('hotkey'):
        scene['hotkey'] = _next_free_hotkey(SCENE_HOTKEY_POOL, _all_used_hotkeys())
    scenes = _load_scenes()
    scenes.append(scene)
    _save_scenes(scenes)
    _refresh_hotkeys()
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
    _refresh_hotkeys()
    return jsonify({'status': 'ok', 'scene': updated})


@bp.route('/api/broadcaster/scenes/<sid>/delete', methods=['POST'])
def delete_scene_route(sid):
    scenes = [s for s in _load_scenes() if s['id'] != sid]
    _save_scenes(scenes)
    _refresh_hotkeys()
    return jsonify({'status': 'ok'})


@bp.route('/api/broadcaster/scenes/<sid>/switch', methods=['POST'])
def switch_scene_route(sid):
    if not _switch_scene(sid):
        return jsonify({'status': 'error', 'message': 'Scene not found.'}), 404
    return jsonify({'status': 'ok'})


@bp.route('/api/broadcaster/scenes/<sid>/hotkey', methods=['POST'])
def set_scene_hotkey_route(sid):
    data = request.json or {}
    hotkey = (data.get('hotkey') or '').strip().lower() or None
    scenes = _load_scenes()
    scene = _find_scene(scenes, sid)
    if not scene:
        return jsonify({'status': 'error', 'message': 'Scene not found.'}), 404
    if hotkey and hotkey in _all_used_hotkeys(except_scene=sid):
        return jsonify({'status': 'error', 'message': f'"{hotkey}" is already assigned to another scene or sound.'}), 400
    scene['hotkey'] = hotkey
    _save_scenes(scenes)
    _refresh_hotkeys()
    return jsonify({'status': 'ok', 'hotkey': hotkey})


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

    sound_hk = _load_sound_hotkeys()
    if not sound_hk.get(safe_name):
        hk = _next_free_hotkey(SOUND_HOTKEY_POOL, _all_used_hotkeys())
        if hk:
            sound_hk[safe_name] = hk
            _save_sound_hotkeys(sound_hk)
    _refresh_hotkeys()
    return jsonify({'status': 'ok', 'filename': safe_name, 'hotkey': sound_hk.get(safe_name)})


@bp.route('/api/broadcaster/sounds/<name>/delete', methods=['POST'])
def delete_sound_route(name):
    path = _sound_path(name)
    if os.path.exists(path):
        os.remove(path)
    sound_hk = _load_sound_hotkeys()
    if sound_hk.pop(name, None) is not None:
        _save_sound_hotkeys(sound_hk)
    _refresh_hotkeys()
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


@bp.route('/api/broadcaster/sounds/<name>/hotkey', methods=['POST'])
def set_sound_hotkey_route(name):
    data = request.json or {}
    hotkey = (data.get('hotkey') or '').strip().lower() or None
    if not os.path.exists(_sound_path(name)):
        return jsonify({'status': 'error', 'message': 'Sound not found.'}), 404
    if hotkey and hotkey in _all_used_hotkeys(except_sound=name):
        return jsonify({'status': 'error', 'message': f'"{hotkey}" is already assigned to another scene or sound.'}), 400
    sound_hk = _load_sound_hotkeys()
    sound_hk[name] = hotkey
    _save_sound_hotkeys(sound_hk)
    _refresh_hotkeys()
    return jsonify({'status': 'ok', 'hotkey': hotkey})


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
    audio_inputs = []
    try:
        import sounddevice as sd
        for i, d in enumerate(sd.query_devices()):
            if d.get('max_output_channels', 0) > 0:
                audio_outputs.append({'index': i, 'label': d['name']})
            if d.get('max_input_channels', 0) > 0:
                audio_inputs.append({'index': i, 'label': d['name']})
    except Exception:
        _log_error('enumerate audio devices')

    return jsonify({
        'cameras': cameras,
        'monitors': monitors,
        'audio_outputs': audio_outputs,
        'audio_inputs': audio_inputs,
    })


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

    for key in ('camera_device_index', 'monitor_index', 'mic_inject_device_index', 'mic_passthrough_source_device_index'):
        if key in data:
            cfg[key] = data[key]
    if 'mirror_camera' in data:
        cfg['mirror_camera'] = bool(data['mirror_camera'])

    _save_config(cfg)
    return jsonify({'status': 'ok', 'config': cfg})
