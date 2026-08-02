"""
Email module — a markup-based HTML email composer with live preview.

The markup text is kept in memory only (module-level _content), for the
lifetime of the running app process:
  - Editing the composer and navigating to a different module and back
    keeps your text, because the Flask process (and this variable) is
    still alive the whole time.
  - Restarting the app resets _content to None, which the frontend reads
    as "nothing saved this session" and falls back to the built-in
    example markup (see EXAMPLE_MARKUP in email.html, and the "Load
    Sample" toolbar button, which can also bring it back on demand
    without restarting).
Nothing is written to disk — this is intentionally session-only, unlike
audio_notes.py's sandbox textarea, which persists across restarts.

Drop this file in modules/ to enable the Email tab in the sidebar;
remove it (or rename it with a leading underscore) to take it out of the
app entirely — no changes to app.py or layout.html are needed either way.
"""
import threading

from flask import Blueprint, render_template, request, jsonify

# ── Module metadata (read by app.py's auto-discovery) ───────────────
NAV_LABEL = 'Email'
NAV_PATH = '/email'
ORDER = 40

bp = Blueprint('email', __name__)

# ── In-memory, session-only state ───────────────────────────────────
_lock = threading.Lock()   # guards _content
# None specifically means "nothing edited/saved this session" — the
# frontend uses that signal to load the built-in example markup instead
# of a blank composer. Once anything is saved (including an explicitly
# emptied composer), _content becomes '' rather than None, and stays that
# way until the app is restarted.
_content = None


def on_load():
    """Called once by app.py when this module is registered at startup —
    explicitly re-asserts the session-only reset, so it's obvious at a
    glance (rather than relying on _content's initial value above) that a
    fresh process always starts back at the example markup."""
    global _content
    _content = None


# ── Page ─────────────────────────────────────────────────────────────

@bp.route('/email')
def email_module():
    with _lock:
        content = _content
    return render_template(
        'email.html',
        content=content or '',
        has_saved_content=(content is not None),
    )


# ── API ──────────────────────────────────────────────────────────────

@bp.route('/api/email/save', methods=['POST'])
def save_route():
    """Called by the frontend on a debounced timer whenever the markup
    changes — see the scheduleSave()/saveContent() pair in email.html.
    Kept only in memory; nothing touches disk here."""
    global _content
    text = (request.json or {}).get('content', '')
    with _lock:
        _content = text
    return jsonify({'status': 'ok'})
