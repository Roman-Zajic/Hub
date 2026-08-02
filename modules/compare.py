"""
Compare module — paste two blocks of text/code and view a line-by-line diff.

Left/right input text is persisted to disk (Compare Data/content.json) so it
survives an app restart, using the same load-on-startup / atomic-write-on-
save pattern as audio_notes.py's sandbox textarea (on_load() loads once at
process start; every save writes to a .tmp file first, then os.replace()s it
over the real file, so a crash mid-write can never leave a half-written,
corrupt file behind).

Drop this file in modules/ to enable the Compare tab in the sidebar;
remove it (or rename it with a leading underscore) to take it out of the
app entirely — no changes to app.py or layout.html are needed either way.
"""
import json
import os
import threading

from flask import Blueprint, render_template, request, jsonify

# ── Module metadata (read by app.py's auto-discovery) ───────────────
NAV_LABEL = 'Compare'
NAV_PATH = '/compare'
ORDER = 20

bp = Blueprint('compare', __name__)

# ── Persistence ──────────────────────────────────────────────────────
DATA_FOLDER = os.path.join(os.getcwd(), 'Compare Data')
os.makedirs(DATA_FOLDER, exist_ok=True)
CONTENT_FILE = os.path.join(DATA_FOLDER, 'content.json')

_lock = threading.Lock()   # guards _left / _right
_left = ''
_right = ''


def _load_content():
    """Returns (left, right) from disk, or ('', '') if nothing's been
    saved yet or the file can't be read (e.g. corrupted by an unrelated
    manual edit)."""
    if os.path.exists(CONTENT_FILE):
        try:
            with open(CONTENT_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return data.get('left', ''), data.get('right', '')
        except Exception:
            return '', ''
    return '', ''


def _save_content(left, right):
    tmp = CONTENT_FILE + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump({'left': left, 'right': right}, f)
        os.replace(tmp, CONTENT_FILE)
    except Exception:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except Exception:
                pass


def on_load():
    """Called once by app.py when this module is registered at startup."""
    global _left, _right
    _left, _right = _load_content()


# ── Page ─────────────────────────────────────────────────────────────

@bp.route('/compare')
def compare_module():
    with _lock:
        left, right = _left, _right
    return render_template('compare.html', left=left, right=right)


# ── API ──────────────────────────────────────────────────────────────

@bp.route('/api/compare/save', methods=['POST'])
def save_route():
    """Called by the frontend on a debounced timer whenever either
    textarea changes — see the scheduleSave()/saveCompareData() pair in
    compare.html, the same shape as audio_notes.html's autosave."""
    global _left, _right
    data = request.json or {}
    left = data.get('left', '')
    right = data.get('right', '')
    with _lock:
        _left, _right = left, right
        _save_content(_left, _right)
    return jsonify({'status': 'ok'})
