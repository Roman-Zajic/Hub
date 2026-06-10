import os
from flask import Flask, render_template, request, jsonify, redirect, url_for
import markdown

app = Flask(__name__)

# Folder where notes are stored
NOTES_FOLDER = os.path.join(os.getcwd(), 'Notes')
if not os.path.exists(NOTES_FOLDER):
    os.makedirs(NOTES_FOLDER)

@app.route('/')
def index():
    return redirect(url_for('notes_module'))

@app.route('/notes')
def notes_module():
    note_files = []
    for root, dirs, files in os.walk(NOTES_FOLDER):
        for file in files:
            if file.endswith('.md'):
                rel_path = os.path.relpath(os.path.join(root, file), NOTES_FOLDER)
                note_files.append(rel_path.replace("\\", "/"))
    return render_template('notes.html', files=sorted(note_files))

@app.route('/notes/load/<path:filename>')
def load_note(filename):
    path = os.path.join(NOTES_FOLDER, filename)
    with open(path, 'r', encoding='utf-8') as f:
        return jsonify({'content': f.read()})

@app.route('/notes/save', methods=['POST'])
def save_note():
    data = request.json
    filename = data.get('filename')
    content = data.get('content')
    path = os.path.join(NOTES_FOLDER, filename)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(content)
    return jsonify({'status': 'success'})

@app.route('/notes/delete', methods=['POST'])
def delete_note():
    filename = request.json.get('filename')
    path = os.path.join(NOTES_FOLDER, filename)
    if os.path.exists(path):
        os.remove(path)
    return jsonify({'status': 'success'})

@app.route('/notes/preview', methods=['POST'])
def preview_note():
    content = request.json.get('content', '')
    # Explicitly adding 'fenced_code' and 'tables' alongside 'nl2br'
    html = markdown.markdown(content, extensions=['fenced_code', 'tables', 'nl2br', 'extra'])
    return jsonify({'html': html})

if __name__ == '__main__':
    app.run(debug=True, port=5000)