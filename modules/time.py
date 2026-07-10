"""
Time module — daily/weekly time-tracking dashboard.

Drop this file in modules/ to enable the Time tab in the sidebar;
remove it (or rename it with a leading underscore) to take it out of the
app entirely — no changes to app.py or layout.html are needed either way.

Everything time-related lives here: the background window-tracking thread,
the JSON data store, and the Flask routes/API. Nothing outside modules/
knows or needs to know this module exists.
"""
import os
import json
import time as time_module
import ctypes
import ctypes.wintypes
import threading
import traceback
from datetime import datetime, timedelta

from flask import Blueprint, render_template, request, jsonify

# ── Module metadata (read by app.py's auto-discovery) ───────────────
NAV_LABEL = 'Time'
NAV_PATH = '/time'
ORDER = 30

bp = Blueprint('time', __name__)


def on_load():
    """Called once by app.py when this module is registered at startup."""
    start_tracking()


def _current_monday():
    today = datetime.now().date()
    return (today - timedelta(days=today.weekday())).strftime('%Y-%m-%d')


# ── File paths ───────────────────────────────────────────────────────
# All JSON data lives in a dedicated "Time Monitor Data" folder.
DATA_FOLDER = os.path.join(os.getcwd(), 'Time Monitor Data')
os.makedirs(DATA_FOLDER, exist_ok=True)

TIME_LOG          = os.path.join(DATA_FOLDER, 'time_log.json')
PROJECTS_FILE     = os.path.join(DATA_FOLDER, 'projects.json')
ALLOCATIONS_FILE  = os.path.join(DATA_FOLDER, 'allocations.json')
DESCRIPTIONS_FILE = os.path.join(DATA_FOLDER, 'time_descriptions.json')
ERROR_LOG         = os.path.join(DATA_FOLDER, 'tracker_error.log')

_lock      = threading.Lock()
_dirty     = False          # write only when data changed
_last_save = 0               # epoch seconds of last flush


def _log_error(context):
    """Record an exception instead of letting it vanish silently. Logging
    itself is wrapped so a logging failure can never crash the caller."""
    try:
        with open(ERROR_LOG, 'a', encoding='utf-8') as f:
            f.write(f'--- {datetime.now().isoformat()} [{context}] ---\n')
            f.write(traceback.format_exc())
            f.write('\n')
    except Exception:
        pass


def _cleanup_stale_tmp_files():
    """Remove any .tmp files left behind by a previous crash mid-write.
    save_json() only ever renames a *fully written* tmp file over the real
    one, so a surviving .tmp is always leftover junk, never live data."""
    for path in (TIME_LOG, PROJECTS_FILE, ALLOCATIONS_FILE, DESCRIPTIONS_FILE):
        tmp = path + '.tmp'
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except Exception:
                pass


# ── Generic JSON helpers ─────────────────────────────────────────────

def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return default
    return default


def save_json(path, data):
    """Thread-safe atomic write. If the write fails partway, the stray .tmp
    is removed instead of being left behind — it's re-raised so the caller
    (e.g. the tracking loop) can log it, but no orphan file lingers."""
    tmp = path + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except Exception:
                pass
        raise


# ── Window tracking ───────────────────────────────────────────────────

def get_active_window():
    try:
        user32   = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        hwnd     = user32.GetForegroundWindow()

        length = user32.GetWindowTextLengthW(hwnd)
        buf    = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title  = buf.value or 'Desktop'

        pid    = ctypes.wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        h_proc = kernel32.OpenProcess(0x1000, False, pid.value)
        app    = 'Unknown'
        if h_proc:
            pbuf = ctypes.create_unicode_buffer(512)
            size = ctypes.wintypes.DWORD(512)
            kernel32.QueryFullProcessImageNameW(h_proc, 0, pbuf, ctypes.byref(size))
            kernel32.CloseHandle(h_proc)
            app = os.path.basename(pbuf.value)
        return app, title
    except Exception:
        return 'Idle', 'Idle'


def start_tracking():
    """Background thread: increments window time every second, flushes every 5s.

    The loop body is wrapped in try/except so a single bad iteration (a
    transient file lock, a flaky win32 call, etc.) is logged to
    'Time Monitor Data/tracker_error.log' and tracking keeps going, instead
    of an unhandled exception silently killing the whole thread forever with
    no visible error.
    """
    def loop():
        global _dirty, _last_save
        log_data = load_json(TIME_LOG, {})   # load once at startup

        while True:
            try:
                app, title = get_active_window()
                today = datetime.now().strftime('%Y-%m-%d')

                with _lock:
                    day = log_data.setdefault(today, {})
                    key = f'{app}|||{title}'
                    day[key] = day.get(key, 0) + 1
                    _dirty = True

                # Flush to disk every 5 seconds at most
                now = time_module.monotonic()
                if _dirty and (now - _last_save) >= 5:
                    with _lock:
                        save_json(TIME_LOG, log_data)
                        _dirty     = False
                        _last_save = now

            except Exception:
                _log_error('tracking loop')
                # Brief backoff so a persistent failure (e.g. disk full)
                # doesn't spin the CPU with rapid-fire retries.
                time_module.sleep(2)

            time_module.sleep(1)

    _cleanup_stale_tmp_files()
    thread = threading.Thread(target=loop, daemon=True)
    thread.start()


# ── Data accessors ─────────────────────────────────────────────────────

def get_day_data(date_str: str):
    """Return logs, projects, and resolved allocations for one day."""
    logs         = load_json(TIME_LOG, {}).get(date_str, {})
    projects     = load_json(PROJECTS_FILE, [])
    allocations  = load_json(ALLOCATIONS_FILE, {'defaults': {}, 'daily': {}})

    day_allocs   = allocations.get('daily', {}).get(date_str, {})
    defaults     = allocations.get('defaults', {})

    # Merge: day-specific overrides defaults
    resolved = {**defaults, **day_allocs}

    return logs, projects, resolved


def get_week_data(monday_str: str):
    """Return per-day logs for a Mon–Sun week starting on monday_str."""
    monday   = datetime.strptime(monday_str, '%Y-%m-%d')
    log_data = load_json(TIME_LOG, {})
    result   = {}
    for i in range(7):
        d = (monday + timedelta(days=i)).strftime('%Y-%m-%d')
        result[d] = log_data.get(d, {})
    return result


def save_allocation(date_str: str, title: str, project_id: str, sub_id: str):
    """
    Save a title→project allocation for a specific day.
    Also updates the global default for that title.
    """
    with _lock:
        data = load_json(ALLOCATIONS_FILE, {'defaults': {}, 'daily': {}})
        data.setdefault('defaults', {})
        data.setdefault('daily', {})
        data['daily'].setdefault(date_str, {})

        entry = {'projectId': project_id, 'subId': sub_id}
        data['daily'][date_str][title] = entry
        data['defaults'][title]        = entry   # becomes new default

        save_json(ALLOCATIONS_FILE, data)


def get_allocations():
    return load_json(ALLOCATIONS_FILE, {'defaults': {}, 'daily': {}})


def save_projects(projects_data):
    with _lock:
        save_json(PROJECTS_FILE, projects_data)


def get_descriptions():
    return load_json(DESCRIPTIONS_FILE, {})


def save_description(date_str: str, project_id: str, sub_id: str, tasks: list, notes: str):
    key  = f'{date_str}|||{project_id}|||{sub_id}'
    with _lock:
        data = load_json(DESCRIPTIONS_FILE, {})
        data[key] = {'tasks': tasks, 'notes': notes}
        save_json(DESCRIPTIONS_FILE, data)


# ── Page ─────────────────────────────────────────────────────────────

@bp.route('/time')
def time_view():
    return render_template('time.html')


# ── Time Tracker API ─────────────────────────────────────────────────

@bp.route('/api/time_data')
def get_time_data_route():
    """Daily view data — ?date=YYYY-MM-DD (defaults to today)."""
    date_str = request.args.get('date', datetime.now().strftime('%Y-%m-%d'))
    logs, projects, allocations = get_day_data(date_str)
    return jsonify({
        'date':        date_str,
        'today':       datetime.now().strftime('%Y-%m-%d'),
        'logs':        logs,
        'projects':    projects,
        'allocations': allocations,
    })


@bp.route('/api/week_data')
def get_week_data_route():
    """Weekly summary — ?start=YYYY-MM-DD (defaults to current Monday)."""
    monday   = request.args.get('start', _current_monday())
    week_logs = get_week_data(monday)
    _, projects, _ = get_day_data(monday)   # projects are global

    # Build allocations for the whole week (merge defaults + each day's overrides)
    alloc_data = get_allocations()
    defaults   = alloc_data.get('defaults', {})
    daily_map  = alloc_data.get('daily', {})

    # For each day return resolved allocations
    week_allocs = {}
    for date_str in week_logs:
        day_ov = daily_map.get(date_str, {})
        week_allocs[date_str] = {**defaults, **day_ov}

    descriptions = get_descriptions()

    return jsonify({
        'monday':       monday,
        'week_logs':    week_logs,
        'week_allocs':  week_allocs,
        'projects':     projects,
        'descriptions': descriptions,
    })


@bp.route('/api/allocation', methods=['POST'])
def save_allocation_route():
    """Save a title→project allocation for a given day."""
    d = request.json
    save_allocation(
        date_str   = d['date'],
        title      = d['title'],
        project_id = d.get('projectId', ''),
        sub_id     = d.get('subId', ''),
    )
    return jsonify({'status': 'ok'})


@bp.route('/api/projects', methods=['GET'])
def get_projects_route():
    return jsonify(load_json(PROJECTS_FILE, []))


@bp.route('/api/projects', methods=['POST'])
def update_projects_route():
    save_projects(request.json)
    return jsonify({'status': 'ok'})


@bp.route('/api/descriptions', methods=['GET'])
def get_descriptions_route():
    return jsonify(get_descriptions())


@bp.route('/api/descriptions', methods=['POST'])
def save_description_route():
    d = request.json
    save_description(
        date_str   = d['date'],
        project_id = d['projectId'],
        sub_id     = d['subId'],
        tasks      = d.get('tasks', []),
        notes      = d.get('notes', ''),
    )
    return jsonify({'status': 'ok'})
