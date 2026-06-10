import os
import json
import time
import ctypes
import ctypes.wintypes
import threading
from datetime import datetime

# Files
TIME_LOG = 'time_log.json'
PROJECTS_FILE = 'projects.json'

_lock = threading.Lock()


def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except:
            return default
    return default


def save_json(path, data):
    with _lock:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)


def get_active_window():
    try:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        hwnd = user32.GetForegroundWindow()

        # Title
        length = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value or "Desktop"

        # App
        pid = ctypes.wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        h_proc = kernel32.OpenProcess(0x1000, False, pid.value)
        app_name = "Unknown"
        if h_proc:
            path_buf = ctypes.create_unicode_buffer(512)
            size = ctypes.wintypes.DWORD(512)
            kernel32.QueryFullProcessImageNameW(h_proc, 0, path_buf, ctypes.byref(size))
            kernel32.CloseHandle(h_proc)
            app_name = os.path.basename(path_buf.value)
        return app_name, title
    except:
        return "Idle", "Idle"


def start_tracking():
    def loop():
        while True:
            app_name, title = get_active_window()
            today = datetime.now().strftime('%Y-%m-%d')

            with _lock:
                data = load_json(TIME_LOG, {})
                if today not in data: data[today] = {}

                key = f"{app_name}|||{title}"
                data[today][key] = data[today].get(key, 0) + 1

                # Save every second (Simplified for this version)
                with open(TIME_LOG, 'w', encoding='utf-8') as f:
                    json.dump(data, f, indent=2)

            time.sleep(1)

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()


def get_stats():
    today = datetime.now().strftime('%Y-%m-%d')
    logs = load_json(TIME_LOG, {}).get(today, {})
    projects = load_json(PROJECTS_FILE, [])
    return logs, projects


def save_projects(projects_data):
    save_json(PROJECTS_FILE, projects_data)