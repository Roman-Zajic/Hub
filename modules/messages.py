"""
Messages module — a single shared text box, synced live between devices on
the same network over a WebSocket. No history, no storage: just whatever
was last submitted.

Drop into modules/ to install; rename with a leading underscore to remove.

Requires: pip install flask-sock --break-system-packages
Requires app.py's app.run(...) to include threaded=True (a websocket stays
open for as long as the tab is loaded, which would otherwise stall the rest
of the app on a single-threaded dev server).
"""
from flask import Blueprint, render_template, request, jsonify
from flask_sock import Sock

NAV_LABEL = 'Messages'
NAV_PATH = '/messages'
ORDER = 50

bp = Blueprint('messages', __name__)
sock = Sock()


@bp.record_once
def _attach_sock(state):
    sock.init_app(state.app)


_text = ''            # the one shared value — nothing else is stored
_clients = set()       # connected websockets


def _broadcast(exclude=None):
    dead = []
    for ws in _clients:
        if ws is exclude:
            continue
        try:
            ws.send(_text)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _clients.discard(ws)


@bp.route('/messages')
def messages_view():
    return render_template('messages.html')


@bp.route('/messages/api/text')
def get_text():
    return jsonify({'text': _text})


@bp.route('/messages/api/text', methods=['POST'])
def set_text():
    global _text
    _text = (request.json or {}).get('text', '')
    _broadcast()
    return jsonify({'status': 'ok'})


@sock.route('/messages/ws', bp=bp)
def messages_ws(ws):
    global _text
    _clients.add(ws)
    ws.send(_text)  # push current value right away
    try:
        while True:
            raw = ws.receive()
            if raw is None:
                break
            _text = raw
            _broadcast(exclude=ws)
    finally:
        _clients.discard(ws)
