"""
Notes module — a markdown note-taking tool with wiki-style [[links]] between notes.

Drop this file in modules/ to enable the Notes tab in the sidebar;
remove it (or rename it with a leading underscore) to take it out of the
app entirely — no changes to app.py or layout.html are needed either way.
"""
import os
import re
from datetime import datetime

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

# Daily notes live in their own dedicated folder inside Notes/, so they
# show up in the tree like any other note but stay grouped together.
DAILY_FOLDER_NAME = 'Daily'

# ── Hidden metadata tag ──────────────────────────────────────────────
# Every note gets a trailing HTML comment recording its creation and last
# modification timestamps, e.g. <!--meta:created=...|modified=...-->.
# It's added/updated here on the server only — load_note() strips it before
# content ever reaches the editor, so it never shows up in the preview or
# in the edit textarea. The only way to see or change it is to open the
# .md file directly outside this app.
_META_RE = re.compile(r'\n?<!--meta:created=(?P<created>[^|]*)\|modified=(?P<modified>.*?)-->\s*\Z', re.DOTALL)

# ── Graph view helpers ───────────────────────────────────────────────
_WIKILINK_RE = re.compile(r'\[\[(.+?)\]\]')


def _resolve_wikilink_target(raw_target, all_paths):
    """Same resolution rules as the front-end's resolveNoteLink(): exact
    path, case-insensitive path, then filename-only match."""
    target = raw_target.split('|', 1)[0].strip()
    if not target:
        return None
    candidate = target if target.endswith('.md') else target + '.md'

    for f in all_paths:
        if f == candidate:
            return f
    for f in all_paths:
        if f.lower() == candidate.lower():
            return f
    base_target = candidate.split('/')[-1].lower()
    for f in all_paths:
        if f.split('/')[-1].lower() == base_target:
            return f
    return None


def _strip_meta(content):
    return _META_RE.sub('', content)


def _extract_meta(content):
    m = _META_RE.search(content)
    if m:
        return m.group('created'), m.group('modified')
    return None, None


def _with_meta(content, created, modified):
    stripped = _strip_meta(content).rstrip('\n')
    return f'{stripped}\n\n<!--meta:created={created}|modified={modified}-->\n'


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
        content = f.read()
    return jsonify({'content': _strip_meta(content)})


@bp.route('/notes/save', methods=['POST'])
def save_note():
    data = request.json
    path = os.path.join(NOTES_FOLDER, data['filename'])
    os.makedirs(os.path.dirname(path), exist_ok=True)

    now = datetime.now().isoformat(timespec='seconds')
    created = now
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                existing_created, _ = _extract_meta(f.read())
            if existing_created:
                created = existing_created
        except Exception:
            pass

    final_content = _with_meta(data['content'], created, now)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(final_content)
    return jsonify({'status': 'ok'})


@bp.route('/notes/delete', methods=['POST'])
def delete_note():
    path = os.path.join(NOTES_FOLDER, request.json['filename'])
    if os.path.exists(path):
        os.remove(path)
    return jsonify({'status': 'ok'})


def _daily_template(date_str):
    pretty = datetime.strptime(date_str, '%Y-%m-%d').strftime('%A, %d %B %Y')
    return f'# {pretty}\n\n## Tasks\n- [ ] \n\n## Daily Notes\n\n'


@bp.route('/notes/daily', methods=['POST'])
def open_daily_note():
    """Idempotent: returns today's daily note, creating it from a template
    on first call each day and simply pointing to it on every later call."""
    date_str = datetime.now().strftime('%Y-%m-%d')
    rel_path = f'{DAILY_FOLDER_NAME}/{date_str}.md'
    full_path = os.path.join(NOTES_FOLDER, DAILY_FOLDER_NAME, f'{date_str}.md')

    created = False
    if not os.path.exists(full_path):
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        now = datetime.now().isoformat(timespec='seconds')
        content = _with_meta(_daily_template(date_str), now, now)
        with open(full_path, 'w', encoding='utf-8') as f:
            f.write(content)
        created = True

    return jsonify({'path': rel_path, 'created': created})


@bp.route('/notes/search')
def search_notes():
    """Case-insensitive substring search across note filenames and file
    content, optionally narrowed to notes last modified within a date
    range. Either a query, a date range, or both may be supplied."""
    query = request.args.get('q', '').strip().lower()
    date_from = request.args.get('from', '').strip()
    date_to = request.args.get('to', '').strip()

    if not query and not date_from and not date_to:
        return jsonify({'matches': []})

    matches = []
    for root, _dirs, filenames in os.walk(NOTES_FOLDER):
        for filename in filenames:
            if not filename.endswith('.md'):
                continue
            full_path = os.path.join(root, filename)
            rel = os.path.relpath(full_path, NOTES_FOLDER).replace('\\', '/')

            try:
                with open(full_path, 'r', encoding='utf-8') as f:
                    content = f.read()
            except Exception:
                continue

            if date_from or date_to:
                _, modified = _extract_meta(content)
                mod_date = (modified or '')[:10]
                if not mod_date:
                    continue
                if date_from and mod_date < date_from:
                    continue
                if date_to and mod_date > date_to:
                    continue

            if query:
                if not (query in rel.lower() or query in _strip_meta(content).lower()):
                    continue

            matches.append(rel)

    return jsonify({'matches': sorted(matches)})


@bp.route('/notes/meta')
def notes_meta():
    """Returns created/modified timestamps for every note — used by the
    date-range calendar to show how many notes were touched on each day."""
    result = []
    for root, _dirs, filenames in os.walk(NOTES_FOLDER):
        for filename in filenames:
            if not filename.endswith('.md'):
                continue
            full_path = os.path.join(root, filename)
            rel = os.path.relpath(full_path, NOTES_FOLDER).replace('\\', '/')
            try:
                with open(full_path, 'r', encoding='utf-8') as f:
                    content = f.read()
            except Exception:
                continue
            created, modified = _extract_meta(content)
            result.append({'path': rel, 'created': created, 'modified': modified})
    return jsonify({'notes': result})


@bp.route('/notes/graph')
def notes_graph():
    """Every note as a node, plus one deduplicated edge per pair of notes
    linked via [[wiki links]] — feeds the graph view."""
    note_files = []
    for root, dirs, files in os.walk(NOTES_FOLDER):
        for file in files:
            if file.endswith('.md'):
                rel = os.path.relpath(os.path.join(root, file), NOTES_FOLDER).replace('\\', '/')
                note_files.append(rel)
    note_files = sorted(note_files)

    nodes, links, seen = [], [], set()

    for path in note_files:
        full_path = os.path.join(NOTES_FOLDER, path)
        try:
            with open(full_path, 'r', encoding='utf-8') as f:
                content = _strip_meta(f.read())
        except Exception:
            content = ''

        folder = path.split('/')[0] if '/' in path else ''
        label = path.split('/')[-1][:-3]  # strip .md
        nodes.append({'id': path, 'label': label, 'folder': folder})

        for m in _WIKILINK_RE.finditer(content):
            target = _resolve_wikilink_target(m.group(1), note_files)
            if target and target != path:
                key = tuple(sorted((path, target)))
                if key not in seen:
                    seen.add(key)
                    links.append({'source': key[0], 'target': key[1]})

    return jsonify({'nodes': nodes, 'links': links})


_ADMONITION_START_RE = re.compile(r'^!!!')
_INDENTED_RE = re.compile(r'^(?: {4,}|\t)')


def _extract_admonitions(content, extract_other):
    """Pulls out `!!! type "Title"` blocks, same as the old regex, but also
    allows blank lines *inside* the block as long as more indented content
    follows — so a callout can hold several paragraphs, not just one run of
    indented lines. A blank line that is NOT followed by further indented
    text ends the block normally, so spacing after a callout is untouched."""
    lines = content.split('\n')
    out = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if _ADMONITION_START_RE.match(line):
            block = [line]
            i += 1
            while i < len(lines):
                if _INDENTED_RE.match(lines[i]):
                    block.append(lines[i])
                    i += 1
                elif lines[i].strip() == '':
                    j = i
                    while j < len(lines) and lines[j].strip() == '':
                        j += 1
                    if j < len(lines) and _INDENTED_RE.match(lines[j]):
                        block.extend(lines[i:j])
                        i = j
                    else:
                        break
                else:
                    break
            out.append(extract_other('\n'.join(block)))
        else:
            out.append(line)
            i += 1
    return '\n'.join(out)


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

    # 1.2. H1 collapse-state marker: "#+ Heading" starts expanded (same as
    # plain "# Heading" — this is just the explicit form), "#- Heading"
    # starts collapsed. Rewritten here into the attr_list syntax python-
    # markdown understands ("# Heading {: .collapsed}"), which attr_list
    # (enabled below) turns into class="collapsed" on the rendered <h1>;
    # attachH1Sections() in notes.html reads that class to pick the
    # section's default state. Done after code-block extraction so a "#-"
    # or "#+" typed inside a code sample is never mistaken for a marker.
    content = re.sub(r'^#\+ +(.+)$', r'# \1', content, flags=re.MULTILINE)
    content = re.sub(r'^#- +(.+)$', r'# \1 {: .collapsed}', content, flags=re.MULTILINE)

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

    def extract_other(text):
        other_blocks.append(text)
        return f"__MAGIC_OTHER_BLOCK_{len(other_blocks) - 1}__"

    def extract_other_match(match):
        return extract_other(match.group(0))

    content = re.sub(r'^\|.*\|(?:\n^\|.*\|)*', extract_other_match, content, flags=re.MULTILINE)
    content = _extract_admonitions(content, extract_other)
    content = re.sub(r'^[-*+] +\[[ xX]\].*(?:\n^[-*+] +\[[ xX]\].*)*', extract_other_match, content, flags=re.MULTILINE)

    # 3. Apply your custom newline logic ONLY to the plain text that is left
    content = re.sub(r'\n+', lambda m: '\n\n' + ('<br>' * (len(m.group(0)) - 1)) + '\n\n', content)

    # 4. Put the blocks back into the text
    # We surround them with \n\n to guarantee Markdown recognizes them as distinct blocks
    for i, block in enumerate(other_blocks):
        content = content.replace(f"__MAGIC_OTHER_BLOCK_{i}__", f"\n\n{block}\n\n")

    for i, code in enumerate(code_blocks):
        content = content.replace(f"__MAGIC_CODE_BLOCK_{i}__", f"\n\n{code}\n\n")

    # 5. Render to HTML
    # attr_list lets a heading opt into starting collapsed, e.g.
    # "# Heading {: .collapsed}" -> <h1 class="collapsed">Heading</h1>.
    # attachH1Sections() in notes.html reads that class to decide the
    # section's default expanded/collapsed state (plain "# Heading" with
    # no marker still defaults to expanded, same as before).
    extensions = ['fenced_code', 'tables', 'extra', 'toc', ChecklistExtension(), 'admonition', 'attr_list']
    # toc_depth=1 restricts [TOC] to H1 headings only — H2/H3 etc. are left
    # out of the generated table of contents.
    extension_configs = {'toc': {'toc_depth': 1}}
    html = markdown.markdown(content, extensions=extensions, extension_configs=extension_configs)

    # External links (http/https) open in a new tab by default. Internal
    # note: links are left alone — the front-end intercepts those clicks
    # itself for in-app navigation, and TOC anchors (#...) stay in-page too.
    html = re.sub(
        r'(<a\s+href="https?://[^"]*")',
        r'\1 target="_blank" rel="noopener noreferrer"',
        html,
    )
    return jsonify({'html': html})
