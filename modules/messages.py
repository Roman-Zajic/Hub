"""
Messages module — a single shared text box, synced live between devices on
the same network over a WebSocket. No history, no storage: just whatever
was last submitted, held in memory.

Drop into modules/ to install; rename with a leading underscore to remove.

Requires: pip install flask-sock --break-system-packages
Requires app.py's app.run(...) to include threaded=True (a websocket stays
open for as long as the tab is loaded, which would otherwise stall the rest
of the app on a single-threaded dev server).
"""
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


_text = ''        # the one shared value — nothing else is stored
_clients = set()   # connected websockets


@bp.route('/messages')
def messages_view():
    return render_template('messages.html')


@sock.route('/messages/ws', bp=bp)
def messages_ws(ws):
    global _text
    _clients.add(ws)
    ws.send(_text)  # push current value right away on connect
    try:
        while True:
            raw = ws.receive()
            if raw is None:
                break
            _text = raw
            dead = []
            for peer in _clients:
                if peer is ws:
                    continue
                try:
                    peer.send(_text)
                except Exception:
                    dead.append(peer)
            for peer in dead:
                _clients.discard(peer)
    finally:
        _clients.discard(ws)
