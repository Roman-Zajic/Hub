import os
import json
import time
import ctypes
import ctypes.wintypes
import threading
from datetime import datetime, timedelta

# ── File paths ───────────────────────────────────────────────
# All JSON data lives in a dedicated "Time Monitor Data" folder.
DATA_FOLDER = os.path.join(os.getcwd(), 'Time Monitor Data')
os.makedirs(DATA_FOLDER, exist_ok=True)

TIME_LOG          = os.path.join(DATA_FOLDER, 'time_log.json')
PROJECTS_FILE     = os.path.join(DATA_FOLDER, 'projects.json')
ALLOCATIONS_FILE  = os.path.join(DATA_FOLDER, 'allocations.json')
DESCRIPTIONS_FILE = os.path.join(DATA_FOLDER, 'time_descriptions.json')

_lock      = threading.Lock()
_dirty     = False          # write only when data changed
_last_save = 0              # epoch seconds of last flush


# ── Generic JSON helpers ─────────────────────────────────────

def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return default
    return default


def save_json(path, data):
    """Thread-safe atomic write."""
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


# ── Window tracking ──────────────────────────────────────────

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
    """Background thread: increments window time every second, flushes every 5s."""
    def loop():
        global _dirty, _last_save
        log_data = load_json(TIME_LOG, {})   # load once at startup

        while True:
            app, title = get_active_window()
            today = datetime.now().strftime('%Y-%m-%d')

            with _lock:
                day = log_data.setdefault(today, {})
                key = f'{app}|||{title}'
                day[key] = day.get(key, 0) + 1
                _dirty = True

            # Flush to disk every 5 seconds at most
            now = time.monotonic()
            if _dirty and (now - _last_save) >= 5:
                with _lock:
                    save_json(TIME_LOG, log_data)
                    _dirty     = False
                    _last_save = now

            time.sleep(1)

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()


# ── Data accessors ───────────────────────────────────────────

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
