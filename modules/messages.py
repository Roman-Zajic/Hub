"""
Messages module — short two-way text messaging between two devices on the
same network (e.g. a company laptop tethered to a personal laptop's hotspot).

Drop this file into modules/ to enable the Messages tab in the sidebar;
remove it (or rename it with a leading underscore) to take it out of the
app entirely — no changes to app.py or layout.html are needed either way.

Transport:
  - Sending always goes over a plain HTTP POST (/messages/api/send), so it
    works even if a websocket handshake is having trouble on a flaky hotspot
    connection.
  - A WebSocket (/messages/ws) pushes new messages to every other connected
    device live, so the log updates on both screens without polling.
  - A manual refresh (/messages/api/history) is available as a fallback in
    case a device's websocket never connects.

Requires the `flask-sock` package (installs `simple-websocket` alongside):
    pip install flask-sock --break-system-packages

One app-wide change is required for this module to work: app.py's
app.run(...) needs threaded=True. A websocket connection is held open for
as long as the Messages tab is loaded on either device, so without
threaded=True the single-threaded dev server would stall every other route
(Notes, Time, etc.) while that tab is open.
"""
import os
import json
import time
import threading

from flask import Blueprint, render_template, request, jsonify
from flask_sock import Sock

# ── Module metadata (read by app.py's auto-discovery) ───────────────
NAV_LABEL = 'Messages'
NAV_PATH = '/messages'
ORDER = 50

bp = Blueprint('messages', __name__)

# flask-sock's Sock needs a live Flask `app` instance to attach itself to,
# but a module file only ever gets a bare Blueprint at import time. Blueprint
# .record_once() defers a callback until the moment app.py actually registers
# this blueprint (state.app is the real app by then), so the websocket setup
# can stay entirely self-contained here instead of requiring an app.py edit.
sock = Sock()


@bp.record_once
def _attach_sock(state):
    sock.init_app(state.app)


# ── Storage ──────────────────────────────────────────────────────────
DATA_FOLDER = os.path.join(os.getcwd(), 'Messages Data')
os.makedirs(DATA_FOLDER, exist_ok=True)
MESSAGES_FILE = os.path.join(DATA_FOLDER, 'messages.json')
MAX_HISTORY = 500  # keep the log from growing forever

_lock = threading.Lock()
_clients = set()  # live simple_websocket connection objects


def _load_messages():
    if os.path.exists(MESSAGES_FILE):
        try:
            with open(MESSAGES_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return []
    return []


def _save_messages(messages):
    tmp = MESSAGES_FILE + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(messages, f, indent=2, ensure_ascii=False)
        os.replace(tmp, MESSAGES_FILE)
    except Exception:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except Exception:
                pass


def _broadcast(payload):
    """Push to every currently-connected client; silently drop any socket
    that errors out (closed/broken) instead of letting one dead peer stop
    delivery to everyone else."""
    data = json.dumps(payload)
    with _lock:
        targets = list(_clients)
    dead = []
    for ws in targets:
        try:
            ws.send(data)
        except Exception:
            dead.append(ws)
    if dead:
        with _lock:
            for ws in dead:
                _clients.discard(ws)


def _handle_new_message(text, sender, client_id):
    """Shared by both the WebSocket receive-loop and the REST /send route,
    so a message sent either way is stored and broadcast identically."""
    text = (text or '').strip()
    if not text:
        return None

    msg = {
        'id':       int(time.time() * 1000),
        'clientId': (client_id or '')[:64],
        'sender':   (sender or 'Unknown').strip()[:40] or 'Unknown',
        'text':     text[:4000],
        'ts':       time.time(),
    }
    with _lock:
        messages = _load_messages()
        messages.append(msg)
        if len(messages) > MAX_HISTORY:
            messages = messages[-MAX_HISTORY:]
        _save_messages(messages)

    _broadcast({'type': 'message', 'message': msg})
    return msg


# ── Page ─────────────────────────────────────────────────────────────

@bp.route('/messages')
def messages_view():
    return render_template('messages.html')


# ── REST: send / history / clear ─────────────────────────────────────

@bp.route('/messages/api/send', methods=['POST'])
def send_message_route():
    d = request.json or {}
    msg = _handle_new_message(d.get('text'), d.get('sender'), d.get('clientId'))
    if msg is None:
        return jsonify({'status': 'error', 'error': 'empty message'}), 400
    return jsonify({'status': 'ok', 'message': msg})


@bp.route('/messages/api/history')
def history_route():
    with _lock:
        return jsonify(_load_messages())


@bp.route('/messages/api/clear', methods=['POST'])
def clear_history_route():
    with _lock:
        _save_messages([])
    _broadcast({'type': 'clear'})
    return jsonify({'status': 'ok'})


# ── WebSocket: live push to the other device(s) ──────────────────────

@sock.route('/messages/ws', bp=bp)
def messages_ws(ws):
    with _lock:
        _clients.add(ws)
    try:
        while True:
            raw = ws.receive()
            if raw is None:   # client disconnected
                break
            try:
                incoming = json.loads(raw)
            except (TypeError, ValueError):
                continue
            _handle_new_message(
                incoming.get('text'),
                incoming.get('sender'),
                incoming.get('clientId'),
            )
    except Exception:
        pass
    finally:
        with _lock:
            _clients.discard(ws)
