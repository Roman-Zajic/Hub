"""
Timers module — multiple concurrent timers (countdown, looping pomodoro,
one-shot alarm), running server-side so they keep going regardless of
which module page is open, and persisted to disk so they survive an app
restart.

Drop this file in modules/ to enable the Timers tab in the sidebar;
remove it (or rename it with a leading underscore) to take it out of the
app entirely.

IMPORTANT: this module also relies on a small snippet added to
layout.html (the floating widget + poll loop) so timers stay visible and
audible on every page, not just /timers itself. See layout.html for the
"TIMERS WIDGET" block.

Nothing here talks to WASAPI/audio hardware — the alarm sound is a plain
browser beep (Web Audio API) triggered client-side when the poll response
reports a freshly-fired event, so this module has zero extra Python
dependencies beyond Flask itself.
"""
import os
import json
import uuid
import threading
from datetime import datetime, timedelta

from flask import Blueprint, render_template, request, jsonify

# ── Module metadata (read by app.py's auto-discovery) ───────────────
NAV_LABEL = 'Timers'
NAV_PATH = '/timers'
ORDER = 60

bp = Blueprint('timers', __name__)

DATA_FOLDER = os.path.join(os.getcwd(), 'Timers Data')
os.makedirs(DATA_FOLDER, exist_ok=True)
STORE_FILE = os.path.join(DATA_FOLDER, 'timers.json')

_lock = threading.Lock()   # guards all reads/writes of _timers
_timers = []               # list of timer dicts, newest last

TYPES = ('countdown', 'pomodoro', 'alarm')


# ── Persistence (atomic write, same pattern as time.py / audio_notes.py) ──

def _load():
    global _timers
    if os.path.exists(STORE_FILE):
        try:
            with open(STORE_FILE, 'r', encoding='utf-8') as f:
                _timers = json.load(f)
        except Exception:
            _timers = []
    else:
        _timers = []


def _save():
    tmp = STORE_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(_timers, f, indent=2)
    os.replace(tmp, STORE_FILE)


def on_load():
    _load()


# ── Helpers ──────────────────────────────────────────────────────────

def _now():
    return datetime.now()


def _iso(dt):
    return dt.isoformat(timespec='seconds')


def _parse(s):
    return datetime.fromisoformat(s)


def _find(tid):
    for t in _timers:
        if t['id'] == tid:
            return t
    return None


def _phase_seconds(t):
    cfg = t['config']
    if t['type'] == 'countdown':
        return cfg['duration_seconds']
    if t['type'] == 'pomodoro':
        return (cfg['work_minutes'] * 60) if t['phase'] == 'work' else (cfg['break_minutes'] * 60)
    return None


def _elapsed_seconds(t, now):
    acc = t.get('accumulated', 0)
    if t['status'] == 'running' and t.get('started_at'):
        acc += (now - _parse(t['started_at'])).total_seconds()
    return acc


def _reset_run_state(t):
    t['accumulated'] = 0
    t['started_at'] = None


def _make_event(t, extra=None):
    ev = {'id': t['id'], 'name': t['name'], 'type': t['type'], 'sound': bool(t.get('sound_enabled'))}
    if extra:
        ev.update(extra)
    return ev


def _next_alarm_target(time_str, now):
    """Always the next upcoming occurrence of this time-of-day — today if
    it hasn't happened yet, otherwise tomorrow. Never more than 24h out."""
    hh, mm = map(int, time_str.split(':'))
    candidate = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


def _advance_countdown(t, now):
    t['status'] = 'completed'
    t['accumulated'] = _phase_seconds(t)
    t['started_at'] = None
    return _make_event(t, {'finished': True})


def _advance_pomodoro(t, now):
    """Pomodoro just alternates work/break forever — no round count, no
    'finished' state. It only ever stops when the user stops it."""
    t['phase'] = 'break' if t['phase'] == 'work' else 'work'
    _reset_run_state(t)
    t['started_at'] = _iso(now)
    return _make_event(t, {'phase_change': True, 'new_phase': t['phase']})


def _tick(t, now):
    """Advances one timer's state to match wall-clock `now`. Returns a
    fired event dict if something completed/changed phase/rang since the
    last tick, else None. Safe to call repeatedly/idempotently — this is
    what makes a timer 'catch up' correctly after the app was closed and
    reopened."""
    typ = t['type']

    if typ in ('countdown', 'pomodoro'):
        if t['status'] != 'running':
            return None
        last_event = None
        for _ in range(500):  # bounded catch-up loop after a long-closed app
            elapsed = _elapsed_seconds(t, now)
            remaining = _phase_seconds(t) - elapsed
            if remaining > 0:
                return last_event
            if typ == 'countdown':
                return _advance_countdown(t, now)
            last_event = _advance_pomodoro(t, now)
        return last_event

    if typ == 'alarm':
        if t['status'] != 'scheduled':
            return None
        if now >= _parse(t['config']['target']):
            t['status'] = 'completed'
            return _make_event(t, {'finished': True})
        return None

    return None


def _tick_all(now):
    events = []
    for t in _timers:
        ev = _tick(t, now)
        if ev:
            events.append(ev)
    if events:
        _save()
    return events


def _public(t):
    """Adds computed (not stored) display fields on top of the raw record
    — elapsed/remaining seconds, phase length — so the frontend never has
    to duplicate the tick math."""
    now = _now()
    out = dict(t)
    if t['type'] in ('countdown', 'pomodoro'):
        phase_len = _phase_seconds(t)
        elapsed = _elapsed_seconds(t, now)
        out['phase_seconds'] = phase_len
        out['elapsed_seconds'] = elapsed
        out['remaining_seconds'] = max(0, phase_len - elapsed)
    elif t['type'] == 'alarm':
        if t['status'] == 'scheduled':
            target = _parse(t['config']['target'])
            out['remaining_seconds'] = max(0, (target - now).total_seconds())
    return out


# ── Validation helpers ───────────────────────────────────────────────

def _clean_config(typ, cfg):
    cfg = cfg or {}
    if typ == 'countdown':
        return {'duration_seconds': max(1, int(cfg.get('duration_seconds', 300)))}
    if typ == 'pomodoro':
        return {
            'work_minutes': max(1, int(cfg.get('work_minutes', 25))),
            'break_minutes': max(1, int(cfg.get('break_minutes', 5))),
        }
    if typ == 'alarm':
        time_str = cfg.get('time', '09:00')
        hh, mm = map(int, time_str.split(':'))
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            raise ValueError('bad time')
        return {'time': f'{hh:02d}:{mm:02d}'}
    raise ValueError('unknown timer type')


# ── Page ─────────────────────────────────────────────────────────────

@bp.route('/timers')
def timers_view():
    return render_template('timers.html', types=TYPES)


# ── API ──────────────────────────────────────────────────────────────

@bp.route('/api/timers/list')
def list_route():
    """The single poll endpoint — used by both the Timers page and the
    global floating-widget script in layout.html. Ticks every timer to
    the current instant, returns the full timer list plus any events
    (completions / phase changes / alarm rings) that fired since the
    previous tick. Events are one-shot: once returned here they are
    cleared server-side, so only ONE caller ever needs to be polling this
    for sound playback to work correctly (see layout.html)."""
    with _lock:
        now = _now()
        events = _tick_all(now)
        timers_out = [_public(t) for t in _timers]
    return jsonify({'timers': timers_out, 'events': events})


@bp.route('/api/timers/create', methods=['POST'])
def create_route():
    data = request.json or {}
    name = (data.get('name') or '').strip()
    typ = data.get('type')
    if typ not in TYPES:
        return jsonify({'status': 'error', 'message': 'Unknown timer type.'}), 400
    try:
        cfg = _clean_config(typ, data.get('config'))
    except Exception:
        return jsonify({'status': 'error', 'message': 'Invalid configuration for this timer type.'}), 400

    if not name:
        name = {'countdown': 'Countdown', 'pomodoro': 'Pomodoro', 'alarm': 'Alarm'}[typ]

    now = _now()
    t = {
        'id': uuid.uuid4().hex[:10],
        'name': name,
        'type': typ,
        'config': cfg,
        'sound_enabled': bool(data.get('sound_enabled', True)),
        'created_at': _iso(now),
    }
    if typ in ('countdown', 'pomodoro'):
        t['status'] = 'stopped'
        t['accumulated'] = 0
        t['started_at'] = None
        if typ == 'pomodoro':
            t['phase'] = 'work'
    elif typ == 'alarm':
        t['config']['target'] = _iso(_next_alarm_target(cfg['time'], now))
        t['status'] = 'scheduled'

    with _lock:
        _timers.append(t)
        _save()
    return jsonify({'status': 'ok', 'timer': _public(t)})


@bp.route('/api/timers/<tid>/start', methods=['POST'])
def start_route(tid):
    with _lock:
        t = _find(tid)
        if not t:
            return jsonify({'status': 'error', 'message': 'Timer not found.'}), 404
        if t['type'] not in ('countdown', 'pomodoro'):
            return jsonify({'status': 'error', 'message': 'This timer type cannot be started/stopped that way.'}), 400
        now = _now()
        if t['status'] in ('stopped', 'completed'):
            _reset_run_state(t)
            if t['type'] == 'pomodoro':
                t['phase'] = 'work'
        t['started_at'] = _iso(now)
        t['status'] = 'running'
        _save()
        return jsonify({'status': 'ok', 'timer': _public(t)})


@bp.route('/api/timers/<tid>/set_remaining', methods=['POST'])
def set_remaining_route(tid):
    """Directly sets how much time is left in the CURRENT run/phase —
    i.e. the elapsed time, not the configured duration — so editing a
    timer that's already running/paused adjusts where it's counting from
    right now rather than resetting it back to the original setup value.
    Works in any state (stopped/paused/running); if running, the clock
    keeps ticking from the new remaining value going forward."""
    data = request.json or {}
    try:
        seconds = max(0, float(data.get('seconds', 0)))
    except (TypeError, ValueError):
        return jsonify({'status': 'error', 'message': 'Invalid value.'}), 400

    with _lock:
        t = _find(tid)
        if not t:
            return jsonify({'status': 'error', 'message': 'Timer not found.'}), 404
        if t['type'] not in ('countdown', 'pomodoro'):
            return jsonify({'status': 'error', 'message': 'Not applicable to this timer type.'}), 400

        phase_len = _phase_seconds(t)
        seconds = min(seconds, phase_len)
        t['accumulated'] = phase_len - seconds
        if t['status'] == 'running':
            t['started_at'] = _iso(_now())
        _save()
        return jsonify({'status': 'ok', 'timer': _public(t)})


@bp.route('/api/timers/<tid>/pause', methods=['POST'])
def pause_route(tid):
    with _lock:
        t = _find(tid)
        if not t:
            return jsonify({'status': 'error', 'message': 'Timer not found.'}), 404
        if t['status'] == 'running':
            t['accumulated'] = _elapsed_seconds(t, _now())
            t['started_at'] = None
            t['status'] = 'paused'
            _save()
        return jsonify({'status': 'ok', 'timer': _public(t)})


@bp.route('/api/timers/<tid>/stop', methods=['POST'])
def stop_route(tid):
    with _lock:
        t = _find(tid)
        if not t:
            return jsonify({'status': 'error', 'message': 'Timer not found.'}), 404
        if t['type'] in ('countdown', 'pomodoro'):
            _reset_run_state(t)
            t['status'] = 'stopped'
            if t['type'] == 'pomodoro':
                t['phase'] = 'work'
        elif t['type'] == 'alarm':
            t['status'] = 'stopped'
        _save()
        return jsonify({'status': 'ok', 'timer': _public(t)})


@bp.route('/api/timers/<tid>/enable', methods=['POST'])
def enable_route(tid):
    """Re-arms a stopped/completed alarm for its next occurrence (today if
    still upcoming, else tomorrow)."""
    with _lock:
        t = _find(tid)
        if not t:
            return jsonify({'status': 'error', 'message': 'Timer not found.'}), 404
        if t['type'] != 'alarm':
            return jsonify({'status': 'error', 'message': 'Not applicable to this timer type.'}), 400
        now = _now()
        t['config']['target'] = _iso(_next_alarm_target(t['config']['time'], now))
        t['status'] = 'scheduled'
        _save()
        return jsonify({'status': 'ok', 'timer': _public(t)})


@bp.route('/api/timers/<tid>/edit', methods=['POST'])
def edit_route(tid):
    data = request.json or {}
    with _lock:
        t = _find(tid)
        if not t:
            return jsonify({'status': 'error', 'message': 'Timer not found.'}), 404

        if 'name' in data:
            name = (data['name'] or '').strip()
            if name:
                t['name'] = name
        if 'sound_enabled' in data:
            t['sound_enabled'] = bool(data['sound_enabled'])
        if 'config' in data:
            try:
                cfg = _clean_config(t['type'], data['config'])
            except Exception:
                return jsonify({'status': 'error', 'message': 'Invalid configuration.'}), 400
            # Applied live — a running countdown/pomodoro keeps its elapsed
            # time and simply recomputes remaining/phase length against the
            # new config on the next tick, rather than being force-stopped.
            t['config'] = cfg
            if t['type'] == 'alarm':
                t['config']['target'] = _iso(_next_alarm_target(cfg['time'], _now()))
                t['status'] = 'scheduled'

        _save()
        return jsonify({'status': 'ok', 'timer': _public(t)})


@bp.route('/api/timers/<tid>/delete', methods=['POST'])
def delete_route(tid):
    global _timers
    with _lock:
        _timers = [t for t in _timers if t['id'] != tid]
        _save()
    return jsonify({'status': 'ok'})
