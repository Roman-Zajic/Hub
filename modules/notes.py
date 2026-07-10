"""
Notes module — a markdown note-taking tool with wiki-style [[links]] between notes.

Drop this file in modules/ to enable the Notes tab in the sidebar;
remove it (or rename it with a leading underscore) to take it out of the
app entirely — no changes to app.py or layout.html are needed either way.
"""
import os
import re

import markdown
from markdown_checklist.extension import ChecklistExtension
from flask import Blueprint, render_template, request, jsonify

# ── Module metadata (read by app.py's auto-discovery) ───────────────
NAV_LABEL = 'Notes'
NAV_PATH = '/notes'
ORDER = 10

bp = Blueprint('notes', __name__)

NOTES_FOLDER = os.path.join(os.getcwd(), 'Notes')
os.makedirs(NOTES_FOLDER, exist_ok=True)


# ── Page ─────────────────────────────────────────────────────────────

@bp.route('/notes')
def notes_module():
    note_files = []
    for root, dirs, files in os.walk(NOTES_FOLDER):
        for file in files:
            if file.endswith('.md'):
                rel = os.path.relpath(os.path.join(root, file), NOTES_FOLDER)
                note_files.append(rel.replace('\\', '/'))
    return render_template('notes.html', files=sorted(note_files))


# ── Notes API ────────────────────────────────────────────────────────

@bp.route('/notes/load/<path:filename>')
def load_note(filename):
    path = os.path.join(NOTES_FOLDER, filename)
    with open(path, 'r', encoding='utf-8') as f:
        return jsonify({'content': f.read()})


@bp.route('/notes/save', methods=['POST'])
def save_note():
    data = request.json
    path = os.path.join(NOTES_FOLDER, data['filename'])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(data['content'])
    return jsonify({'status': 'ok'})


@bp.route('/notes/delete', methods=['POST'])
def delete_note():
    path = os.path.join(NOTES_FOLDER, request.json['filename'])
    if os.path.exists(path):
        os.remove(path)
    return jsonify({'status': 'ok'})


def _convert_wiki_link(match):
    """[[Note Name]] or [[Note Name|Display Text]] -> [Display Text](note:Note Name)"""
    inner = match.group(1).strip()
    if '|' in inner:
        target, label = inner.split('|', 1)
    else:
        target, label = inner, inner
    return f'[{label.strip()}](note:{target.strip()})'


@bp.route('/notes/preview', methods=['POST'])
def preview_note():
    content = request.json.get('content', '')

    # 1. Safely extract Code Blocks FIRST
    # We replace them with a magic placeholder so they are completely protected
    code_blocks = []

    def extract_code(match):
        code_blocks.append(match.group(0))
        return f"__MAGIC_CODE_BLOCK_{len(code_blocks) - 1}__"

    # Match ``` followed by anything (including newlines) until the next ```
    content = re.sub(r'```[\s\S]*?```', extract_code, content)

    # 1.5. Convert [[wiki links]] into a custom "note:" scheme link, so the
    # front-end can intercept clicks and jump between notes. Inline
    # `single-backtick` code spans are protected first, so literal [[ ]]
    # syntax shown as an example in inline code isn't converted into a link.
    inline_code = []

    def extract_inline_code(match):
        inline_code.append(match.group(0))
        return f"__MAGIC_INLINE_CODE_{len(inline_code) - 1}__"

    content = re.sub(r'`[^`\n]+`', extract_inline_code, content)
    content = re.sub(r'\[\[(.+?)\]\]', _convert_wiki_link, content)
    for i, code in enumerate(inline_code):
        content = content.replace(f"__MAGIC_INLINE_CODE_{i}__", code)

    # 2. Safely extract other complex blocks (Tables, Admonitions, Checklists)
    # Note: these patterns intentionally do NOT consume the newline that
    # follows the block's final line, so a single blank line placed after a
    # table/admonition/checklist behaves the same as a single blank line
    # anywhere else in the document (see step 3 below).
    other_blocks = []

    def extract_other(match):
        other_blocks.append(match.group(0))
        return f"__MAGIC_OTHER_BLOCK_{len(other_blocks) - 1}__"

    content = re.sub(r'^\|.*\|(?:\n^\|.*\|)*', extract_other, content, flags=re.MULTILINE)
    content = re.sub(r'^!!!.*(?:\n(?: {4,}|\t).*)*', extract_other, content, flags=re.MULTILINE)
    content = re.sub(r'^[-*+] +\[[ xX]\].*(?:\n^[-*+] +\[[ xX]\].*)*', extract_other, content, flags=re.MULTILINE)

    # 3. Apply your custom newline logic ONLY to the plain text that is left
    content = re.sub(r'\n+', lambda m: '\n\n' + ('<br>' * (len(m.group(0)) - 1)) + '\n\n', content)

    # 4. Put the blocks back into the text
    # We surround them with \n\n to guarantee Markdown recognizes them as distinct blocks
    for i, block in enumerate(other_blocks):
        content = content.replace(f"__MAGIC_OTHER_BLOCK_{i}__", f"\n\n{block}\n\n")

    for i, code in enumerate(code_blocks):
        content = content.replace(f"__MAGIC_CODE_BLOCK_{i}__", f"\n\n{code}\n\n")

    # 5. Render to HTML
    extensions = ['fenced_code', 'tables', 'extra', 'toc', ChecklistExtension(), 'admonition']
    html = markdown.markdown(content, extensions=extensions)
    return jsonify({'html': html})
