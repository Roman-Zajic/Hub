"""
Data Analysis module (formerly "Timesheet").

Paste TSV data (e.g. copied straight out of Excel) and the server will:
  1. Detect each column's type (text / number / percentage / date / time)
     from its actual values, regardless of what format it was pasted in.
  2. Re-express every value in one standard display format per type:
       date        -> yyyy-mm-dd
       number      -> #,##0.00
       percentage  -> 0%
       time        -> hh:mm:ss
       text        -> left as-is
  3. For every date column, compute extra "calendar" columns (Year / Month /
     Week) against two hardcoded fiscal calendars — see the SETUP section
     below. These extension columns are appended to the table and are
     hidden by default (see DEBUG_SHOW_CALENDAR_COLUMNS).

Everything else (filtering, sorting, the excel-style column dropdowns) is
client-side against the JSON this module returns — there is no groupby/
pivot step here any more; the report is the full standardized table.

Drop this file into modules/ to install it (see app.py header comment).
"""
import json
import os
import re
import threading
from datetime import datetime, timedelta

from flask import Blueprint, render_template, request, jsonify

bp = Blueprint('data_analysis', __name__)

NAV_LABEL = 'Data Analysis'
NAV_PATH = '/data_analysis'
ORDER = 20

# ── Persistence for saved Visualization presets ─────────────────────
# Just a name -> definition-text map. Atomic write, same pattern used
# throughout this app (e.g. compare.py, timers.py).
DATA_FOLDER = os.path.join(os.getcwd(), 'Data Analysis Data')
os.makedirs(DATA_FOLDER, exist_ok=True)
VISUALIZATIONS_FILE = os.path.join(DATA_FOLDER, 'visualizations.json')
_viz_lock = threading.Lock()


def _load_visualizations():
    if os.path.exists(VISUALIZATIONS_FILE):
        try:
            with open(VISUALIZATIONS_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def _save_visualizations(data):
    tmp = VISUALIZATIONS_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, VISUALIZATIONS_FILE)


# ══════════════════════════════════════════════════════════════════════
#  SETUP — edit these to change behavior. There is deliberately no UI for
#  any of this: the calendars are a fixed, shared definition, not a
#  per-report choice.
# ══════════════════════════════════════════════════════════════════════

# When True, the EY Calendar / Standard Calendar extension columns are
# shown in the table by default instead of hidden. They are ALWAYS
# computed either way — this only controls default visibility, purely
# for debugging/verification. Set back to False for normal day-to-day use.
DEBUG_SHOW_CALENDAR_COLUMNS = False

# A value must parse successfully as a given type for at least this
# fraction of a column's non-empty cells for the column to be classified
# as that type. Below this, the column falls back to 'text'.
TYPE_DETECTION_MIN_RATIO = 0.6

# ── Standard Calendar ────────────────────────────────────────────────
# Plain Gregorian calendar. Week-start convention below uses
# 0=Sunday, 1=Monday, ..., 6=Saturday (kept the same convention as the
# EY calendar's own config, for consistency).
STANDARD_WEEK_STARTS_ON = 1  # Monday

# ── EY Calendar (fiscal 5-4-4) ───────────────────────────────────────
EY_WEEK_STARTS_ON = 6  # Saturday

# Any date on/after the true first day of the fiscal year works — it is
# snapped back to the start of its own week (per EY_WEEK_STARTS_ON) before
# anything else is calculated. Every other fiscal week/period boundary, in
# any year, is calculated outward from that one snapped anchor.
EY_FISCAL_YEAR_ANCHOR = '2026-07-04'
EY_ANCHOR_FISCAL_YEAR = 2027

# Weeks per fiscal month/period, in order, for one fiscal year (5-4-4
# pattern x 4 quarters = 52 weeks). Change this if your company uses a
# different split (e.g. 4-4-5).
EY_MONTH_WEEK_PATTERN = [5, 4, 4, 5, 4, 4, 5, 4, 4, 5, 4, 4]

# Fiscal years that have an extra 53rd week, mapped to which period (1-12)
# absorbs it.
EY_LEAP_WEEK_FISCAL_YEARS = {2026: 12}


# ══════════════════════════════════════════════════════════════════════
#  Type detection / parsing
# ══════════════════════════════════════════════════════════════════════

_DATE_FORMATS = [
    '%Y-%m-%d', '%Y/%m/%d', '%Y.%m.%d',
    '%d %b %Y', '%d %B %Y',
    '%b %d, %Y', '%B %d, %Y', '%b %d %Y', '%B %d %Y',
    '%m/%d/%Y', '%d/%m/%Y',
    '%m-%d-%Y', '%d-%m-%Y',
    '%d.%m.%Y',
]

_TIME_RE = re.compile(r'^(\d{1,2}):(\d{2})(?::(\d{2}))?\s*(AM|PM|am|pm)?$')
_NUM_CORE_RE = re.compile(r'^[+-]?\d+(\.\d+)?$')


def _try_parse_date(s):
    s = (s or '').strip()
    if not s:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _try_parse_time(s):
    s = (s or '').strip()
    m = _TIME_RE.match(s)
    if not m:
        return None
    h, mi, se, ampm = m.groups()
    h, mi = int(h), int(mi)
    se = int(se) if se else 0
    if ampm:
        ampm = ampm.upper()
        if ampm == 'PM' and h != 12:
            h += 12
        if ampm == 'AM' and h == 12:
            h = 0
    if h > 23 or mi > 59 or se > 59:
        return None
    return timedelta(hours=h, minutes=mi, seconds=se)


def _try_parse_percentage(s):
    s = (s or '').strip()
    if '%' not in s:
        return None
    neg = s.startswith('(') and s.endswith(')')
    core = s[1:-1] if neg else s
    core = core.replace('%', '').replace(',', '').strip()
    if not core or not _NUM_CORE_RE.match(core):
        return None
    v = float(core) / 100.0
    return -abs(v) if neg else v


def _try_parse_number(s):
    s = (s or '').strip()
    if not s or '%' in s:
        return None
    neg = s.startswith('(') and s.endswith(')')
    core = s[1:-1] if neg else s
    core = core.replace('$', '').replace(',', '').strip()
    if not core or not _NUM_CORE_RE.match(core):
        return None
    v = float(core)
    return -abs(v) if neg else v


def _detect_column_type(values):
    non_empty = [v for v in values if v.strip() != '']
    total = len(non_empty)
    if total == 0:
        return 'text'

    counts = {'date': 0, 'time': 0, 'percentage': 0, 'number': 0}
    for v in non_empty:
        if _try_parse_date(v) is not None:
            counts['date'] += 1
        if _try_parse_time(v) is not None:
            counts['time'] += 1
        if _try_parse_percentage(v) is not None:
            counts['percentage'] += 1
        if _try_parse_number(v) is not None:
            counts['number'] += 1

    best_type, best_ratio = 'text', 0.0
    for t in ('date', 'time', 'percentage', 'number'):  # priority order
        ratio = counts[t] / total
        if ratio > best_ratio and ratio >= TYPE_DETECTION_MIN_RATIO:
            best_type, best_ratio = t, ratio
    return best_type


def _standardize_cell(raw, coltype):
    """Returns {v: original raw string, t: effective type, d: standardized
    display string, s: sort key, dt: parsed datetime (date cells only,
    stripped before the response is sent)}."""
    if raw == '':
        return {'v': raw, 't': coltype, 'd': '', 's': ''}

    if coltype == 'date':
        dt = _try_parse_date(raw)
        if dt is None:
            return {'v': raw, 't': 'text', 'd': raw, 's': raw.lower()}
        d0 = datetime(dt.year, dt.month, dt.day)
        disp = d0.strftime('%Y-%m-%d')
        return {'v': raw, 't': 'date', 'd': disp, 's': disp, 'dt': d0}

    if coltype == 'time':
        td = _try_parse_time(raw)
        if td is None:
            return {'v': raw, 't': 'text', 'd': raw, 's': raw.lower()}
        total = int(td.total_seconds())
        h, rem = divmod(total, 3600)
        m, s = divmod(rem, 60)
        disp = f'{h:02d}:{m:02d}:{s:02d}'
        return {'v': raw, 't': 'time', 'd': disp, 's': total}

    if coltype == 'percentage':
        v = _try_parse_percentage(raw)
        if v is None:
            return {'v': raw, 't': 'text', 'd': raw, 's': raw.lower()}
        disp = f'{round(v * 100)}%'
        return {'v': raw, 't': 'percentage', 'd': disp, 's': v}

    if coltype == 'number':
        v = _try_parse_number(raw)
        if v is None:
            return {'v': raw, 't': 'text', 'd': raw, 's': raw.lower()}
        disp = f'{v:,.2f}'
        return {'v': raw, 't': 'number', 'd': disp, 's': v}

    return {'v': raw, 't': 'text', 'd': raw, 's': raw.lower()}


# ══════════════════════════════════════════════════════════════════════
#  Calendars — Standard (plain Gregorian) and EY (fiscal 5-4-4)
# ══════════════════════════════════════════════════════════════════════

def _week_start(dt, week_starts_on):
    """dt -> the start of its week, per a 0=Sunday..6=Saturday convention."""
    sun0 = (dt.weekday() + 1) % 7  # python Monday=0..Sunday=6 -> Sunday=0..Saturday=6
    diff = (sun0 - week_starts_on) % 7
    return dt - timedelta(days=diff)


def _standard_bucket(dt):
    year = str(dt.year)
    month = f'{dt.year:04d}-{dt.month:02d}'
    ws = _week_start(dt, STANDARD_WEEK_STARTS_ON)
    week = f'Week of {ws.strftime("%Y-%m-%d")}'
    return year, month, week


def _ey_fiscal_year_length(fy):
    return 52 + (1 if fy in EY_LEAP_WEEK_FISCAL_YEARS else 0)


def _ey_locate_fiscal_year_week(weeks_offset):
    fy = EY_ANCHOR_FISCAL_YEAR
    remaining = weeks_offset
    if remaining >= 0:
        while remaining >= _ey_fiscal_year_length(fy):
            remaining -= _ey_fiscal_year_length(fy)
            fy += 1
    else:
        while remaining < 0:
            fy -= 1
            remaining += _ey_fiscal_year_length(fy)
    return fy, remaining


def _ey_locate_period(fiscal_year, week_index):
    pattern = list(EY_MONTH_WEEK_PATTERN)
    leap_period = EY_LEAP_WEEK_FISCAL_YEARS.get(fiscal_year)
    if leap_period:
        pattern[leap_period - 1] += 1
    cum = 0
    for i, wk in enumerate(pattern):
        if week_index < cum + wk:
            return i + 1
        cum += wk
    return len(pattern)


_EY_ANCHOR_DT = None


def _ey_bucket(dt):
    global _EY_ANCHOR_DT
    try:
        if _EY_ANCHOR_DT is None:
            anchor_dt = datetime.strptime(EY_FISCAL_YEAR_ANCHOR, '%Y-%m-%d')
            _EY_ANCHOR_DT = _week_start(anchor_dt, EY_WEEK_STARTS_ON)
        ws = _week_start(dt, EY_WEEK_STARTS_ON)
        weeks_offset = round((ws - _EY_ANCHOR_DT).days / 7)
        fy, week_index = _ey_locate_fiscal_year_week(weeks_offset)
        period = _ey_locate_period(fy, week_index)
        # Same naming convention as the Standard calendar (plain year,
        # "YYYY-MM"-shaped month) rather than an "FY2027 P03" style label —
        # only the underlying fiscal-year/period numbers differ.
        year_label = str(fy)
        month_label = f'{fy:04d}-{period:02d}'
        week_label = f'Week of {ws.strftime("%Y-%m-%d")}'
        return year_label, month_label, week_label
    except Exception:
        return '', '', ''


# ══════════════════════════════════════════════════════════════════════
#  TSV processing
# ══════════════════════════════════════════════════════════════════════

def _process_tsv(text):
    lines = (text or '').replace('\r\n', '\n').replace('\r', '\n').split('\n')
    while lines and lines[-1].strip() == '':
        lines.pop()
    if not lines:
        return {'columns': [], 'rows': []}

    headers = [h.strip() for h in lines[0].split('\t')]
    data_lines = [ln for ln in lines[1:] if ln.strip() != '']
    raw_rows = [ln.split('\t') for ln in data_lines]

    col_values = []
    for ci in range(len(headers)):
        col_values.append([(r[ci].strip() if ci < len(r) else '') for r in raw_rows])

    col_types = [_detect_column_type(vals) for vals in col_values]

    columns = [
        {'key': f'c{ci}', 'label': headers[ci], 'type': col_types[ci], 'hidden': False}
        for ci in range(len(headers))
    ]

    out_rows = []
    for r in raw_rows:
        row = {}
        for ci in range(len(headers)):
            raw_v = r[ci].strip() if ci < len(r) else ''
            row[f'c{ci}'] = _standardize_cell(raw_v, col_types[ci])
        out_rows.append(row)

    # Calendar extension columns — one set per date-type column.
    date_col_indices = [ci for ci in range(len(headers)) if col_types[ci] == 'date']
    ext_columns = []
    for ci in date_col_indices:
        key = f'c{ci}'
        label = headers[ci]
        specs = [
            ('std_year', f'{label} (Standard Year)'),
            ('std_month', f'{label} (Standard Month)'),
            ('std_week', f'{label} (Standard Week)'),
            ('ey_year', f'{label} (EY Year)'),
            ('ey_month', f'{label} (EY Month)'),
            ('ey_week', f'{label} (EY Week)'),
        ]
        for suffix, ext_label in specs:
            ext_columns.append({
                'key': f'{key}__{suffix}', 'label': ext_label, 'type': 'text',
                'hidden': not DEBUG_SHOW_CALENDAR_COLUMNS,
            })

        for row in out_rows:
            dt = row[key].get('dt')
            if dt is not None:
                sy, sm, sw = _standard_bucket(dt)
                ey_y, ey_m, ey_w = _ey_bucket(dt)
            else:
                sy = sm = sw = ey_y = ey_m = ey_w = ''
            row[f'{key}__std_year'] = {'v': sy, 't': 'text', 'd': sy, 's': sy}
            row[f'{key}__std_month'] = {'v': sm, 't': 'text', 'd': sm, 's': sm}
            row[f'{key}__std_week'] = {'v': sw, 't': 'text', 'd': sw, 's': sw}
            row[f'{key}__ey_year'] = {'v': ey_y, 't': 'text', 'd': ey_y, 's': ey_y}
            row[f'{key}__ey_month'] = {'v': ey_m, 't': 'text', 'd': ey_m, 's': ey_m}
            row[f'{key}__ey_week'] = {'v': ey_w, 't': 'text', 'd': ey_w, 's': ey_w}

    # Strip the internal (non-JSON-friendly) parsed datetime before returning.
    for row in out_rows:
        for ci in date_col_indices:
            row[f'c{ci}'].pop('dt', None)

    return {'columns': columns + ext_columns, 'rows': out_rows}


# ── Page ─────────────────────────────────────────────────────────────

@bp.route('/data_analysis')
def data_analysis_view():
    return render_template('data_analysis.html')


# ── API ──────────────────────────────────────────────────────────────

@bp.route('/api/data_analysis/process', methods=['POST'])
def process_route():
    text = (request.json or {}).get('text', '')
    return jsonify(_process_tsv(text))


# ── Visualization presets ────────────────────────────────────────────
# Just plain text (the same DSL the Visualization editor works with) —
# no parsing/validation happens server-side; it's re-parsed client-side
# every time it's loaded, against whatever the Clean Data table looks
# like at that moment.

@bp.route('/api/data_analysis/visualizations', methods=['GET'])
def list_visualizations_route():
    return jsonify(_load_visualizations())


@bp.route('/api/data_analysis/visualizations/save', methods=['POST'])
def save_visualization_route():
    data = request.json or {}
    name = (data.get('name') or '').strip()
    definition = data.get('definition', '')
    if not name:
        return jsonify({'status': 'error', 'message': 'A name is required.'}), 400
    with _viz_lock:
        viz = _load_visualizations()
        viz[name] = definition
        _save_visualizations(viz)
    return jsonify({'status': 'ok'})


@bp.route('/api/data_analysis/visualizations/delete', methods=['POST'])
def delete_visualization_route():
    name = (request.json or {}).get('name', '').strip()
    with _viz_lock:
        viz = _load_visualizations()
        viz.pop(name, None)
        _save_visualizations(viz)
    return jsonify({'status': 'ok'})
