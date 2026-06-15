import os
import re
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify, redirect, url_for
import markdown
import tracker

app = Flask(__name__)
tracker.start_tracking()

NOTES_FOLDER = os.path.join(os.getcwd(), 'Notes')
os.makedirs(NOTES_FOLDER, exist_ok=True)


# ── Helpers ──────────────────────────────────────────────────

def current_monday():
    today = datetime.now().date()
    return (today - timedelta(days=today.weekday())).strftime('%Y-%m-%d')


# ── Shell / routing ──────────────────────────────────────────

@app.route('/')
def index():
    return redirect(url_for('notes_module'))

@app.route('/notes')
def notes_module():
    note_files = []
    for root, dirs, files in os.walk(NOTES_FOLDER):
        for file in files:
            if file.endswith('.md'):
                rel = os.path.relpath(os.path.join(root, file), NOTES_FOLDER)
                note_files.append(rel.replace('\\', '/'))
    return render_template('notes.html', files=sorted(note_files))

@app.route('/compare')
def compare_module():
    return render_template('compare.html')

@app.route('/time')
def time_module():
    return render_template('time.html')


# ── Notes API ────────────────────────────────────────────────

@app.route('/notes/load/<path:filename>')
def load_note(filename):
    path = os.path.join(NOTES_FOLDER, filename)
    with open(path, 'r', encoding='utf-8') as f:
        return jsonify({'content': f.read()})

@app.route('/notes/save', methods=['POST'])
def save_note():
    data     = request.json
    path     = os.path.join(NOTES_FOLDER, data['filename'])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(data['content'])
    return jsonify({'status': 'ok'})

@app.route('/notes/delete', methods=['POST'])
def delete_note():
    path = os.path.join(NOTES_FOLDER, request.json['filename'])
    if os.path.exists(path):
        os.remove(path)
    return jsonify({'status': 'ok'})

@app.route('/notes/preview', methods=['POST'])
def preview_note():
    content = request.json.get('content', '')
    content = re.sub(
        r'\n{3,}',
        lambda m: '\n\n' + ('<br>' * (len(m.group(0)) - 2)) + '\n\n',
        content
    )
    html = markdown.markdown(content, extensions=['fenced_code', 'tables', 'extra'])
    return jsonify({'html': html})


# ── Time Tracker API ─────────────────────────────────────────

@app.route('/api/time_data')
def get_time_data():
    """Daily view data — ?date=YYYY-MM-DD (defaults to today)."""
    date_str = request.args.get('date', datetime.now().strftime('%Y-%m-%d'))
    logs, projects, allocations = tracker.get_day_data(date_str)
    return jsonify({
        'date':        date_str,
        'today':       datetime.now().strftime('%Y-%m-%d'),
        'logs':        logs,
        'projects':    projects,
        'allocations': allocations,
    })

@app.route('/api/week_data')
def get_week_data():
    """Weekly summary — ?start=YYYY-MM-DD (defaults to current Monday)."""
    monday   = request.args.get('start', current_monday())
    week_logs = tracker.get_week_data(monday)
    _, projects, _ = tracker.get_day_data(monday)   # projects are global

    # Build allocations for the whole week (merge defaults + each day's overrides)
    alloc_data = tracker.get_allocations()
    defaults   = alloc_data.get('defaults', {})
    daily_map  = alloc_data.get('daily', {})

    # For each day return resolved allocations
    week_allocs = {}
    for date_str in week_logs:
        day_ov = daily_map.get(date_str, {})
        week_allocs[date_str] = {**defaults, **day_ov}

    descriptions = tracker.get_descriptions()

    return jsonify({
        'monday':       monday,
        'week_logs':    week_logs,
        'week_allocs':  week_allocs,
        'projects':     projects,
        'descriptions': descriptions,
    })

@app.route('/api/allocation', methods=['POST'])
def save_allocation():
    """Save a title→project allocation for a given day."""
    d = request.json
    tracker.save_allocation(
        date_str   = d['date'],
        title      = d['title'],
        project_id = d.get('projectId', ''),
        sub_id     = d.get('subId', ''),
    )
    return jsonify({'status': 'ok'})

@app.route('/api/projects', methods=['GET'])
def get_projects():
    from tracker import load_json, PROJECTS_FILE
    return jsonify(load_json(PROJECTS_FILE, []))

@app.route('/api/projects', methods=['POST'])
def update_projects():
    tracker.save_projects(request.json)
    return jsonify({'status': 'ok'})

@app.route('/api/descriptions', methods=['GET'])
def get_descriptions():
    return jsonify(tracker.get_descriptions())

@app.route('/api/descriptions', methods=['POST'])
def save_description():
    d = request.json
    tracker.save_description(
        date_str   = d['date'],
        project_id = d['projectId'],
        sub_id     = d['subId'],
        tasks      = d.get('tasks', []),
        notes      = d.get('notes', ''),
    )
    return jsonify({'status': 'ok'})


if __name__ == '__main__':
    app.run(debug=True, port=5001, use_reloader=False)
