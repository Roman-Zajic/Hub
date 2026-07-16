"""
Messages module — a single shared text box plus simple file drop, synced
live between devices on the same network over a WebSocket. Nothing is
stored server-side: text is kept only as "the last value" in memory, and
files are relayed straight through without ever touching disk.

Drop into modules/ to install; rename with a leading underscore to remove.

Requires: pip install flask-sock --break-system-packages
Requires app.py's app.run(...) to include threaded=True (a websocket stays
open for as long as the tab is loaded, which would otherwise stall the rest
of the app on a single-threaded dev server).
"""
import json

from flask import Blueprint, render_template
from flask_sock import Sock

NAV_LABEL = 'Messages'
NAV_PATH = '/messages'
ORDER = 50

bp = Blueprint('messages', __name__)
sock = Sock()


@bp.record_once
def _attach_sock(state):
    sock.init_app(state.app)


_text = ''        # the one shared text value — nothing else is stored
_clients = set()   # connected websockets


@bp.route('/messages')
def messages_view():
    return render_template('messages.html')


@sock.route('/messages/ws', bp=bp)
def messages_ws(ws):
    global _text
    _clients.add(ws)
    ws.send(json.dumps({'type': 'text', 'text': _text}))  # sync current value on connect
    try:
        while True:
            raw = ws.receive()
            if raw is None:
                break
            try:
                msg = json.loads(raw)
            except (TypeError, ValueError):
                continue

            if msg.get('type') == 'text':
                _text = msg.get('text', '')
            # 'file' messages are relayed only, never stored

            dead = []
            for peer in _clients:
                if peer is ws:
                    continue
                try:
                    peer.send(raw)
                except Exception:
                    dead.append(peer)
            for peer in dead:
                _clients.discard(peer)
    finally:
        _clients.discard(ws)
