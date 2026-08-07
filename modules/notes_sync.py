"""
Notes (Sync) module — the same markdown note-taking experience as the plain
Notes module (wiki-style [[links]], daily notes, graph view, date-range
search), but backed by a GitHub repository instead of purely local files.

This is a fully separate vault from the plain Notes module: separate
storage folder, separate [[links]] namespace, separate graph, separate
Daily/ folder. Folder/file names may overlap between the two modules with
no collision, because they simply live in different folders on disk (and
sync to a different GitHub repo/branch than anything else).

── Sync model (chosen deliberately over a couple of alternatives) ───────
  - GitHub REST API only — no git binary / GitPython needed. A "pull" is
    one Git Trees API call (lists every file + blob sha in the repo) plus
    one Blobs API call per file that actually changed; a "push" is one
    Contents API PUT (create/update) or DELETE call per changed file.
  - Manual sync only: nothing talks to GitHub until the "Sync Now" button
    is clicked. Local edits autosave locally immediately, same as plain
    Notes, and are simply flagged "unsynced" (see /notes_sync/status)
    until the next Sync Now.
  - There is NO in-app settings screen. Configuration lives entirely in
    "Notes (Sync) Data/config.json", which you edit by hand in a text
    editor. A placeholder template is written there automatically the
    first time this module starts if the file doesn't exist yet — open
    it, fill in your token/repo/branch/subdir, save, and reload the page.
    This file contains your token in PLAIN TEXT on local disk. If this
    project folder is itself under git version control, add
    "Notes (Sync) Data/" to your .gitignore so the token is never
    committed anywhere — and never paste a real token into a chat
    conversation or anywhere else outside this file.

── Conflict handling ─────────────────────────────────────────────────
A note that changed BOTH locally and on GitHub since the last successful
sync is never silently overwritten in either direction. The remote
version is written alongside your local copy as
"<name>.conflict-<timestamp>.md" so you can compare and merge by hand;
your local edits (and its "unsynced" flag) are left exactly as they were
— they'll be pushed as-is on the next Sync Now once you're happy with
them. A note deleted on GitHub while you'd edited it locally is kept
locally and will simply be re-created on GitHub on the next push.

Third-party requirement: pip install requests --break-system-packages

Drop this file in modules/ to enable the Notes (Sync) tab in the
sidebar; remove it (or rename it with a leading underscore) to take it
out of the app entirely — no changes to app.py or layout.html needed.
"""
import base64
import hashlib
import json
import os
import re
import threading
import urllib.parse
from datetime import datetime
from html import escape as _html_escape

import markdown
from markdown_checklist.extension import ChecklistExtension
from flask import Blueprint, render_template, request, jsonify, send_from_directory

# ── Module metadata (read by app.py's auto-discovery) ───────────────
NAV_LABEL = 'Notes (Sync)'
NAV_PATH = '/notes_sync'
ORDER = 11  # right after the plain Notes module (10)

bp = Blueprint('notes_sync', __name__)

# ── Storage layout ───────────────────────────────────────────────────
DATA_FOLDER = os.path.join(os.getcwd(), 'Notes (Sync) Data')
VAULT_FOLDER = os.path.join(DATA_FOLDER, 'vault')   # local mirror of the GitHub repo's notes
os.makedirs(VAULT_FOLDER, exist_ok=True)

CONFIG_FILE = os.path.join(DATA_FOLDER, 'config.json')     # {token, repo, branch, subdir, last_synced_at}
STATE_FILE = os.path.join(DATA_FOLDER, 'sync_state.json')  # {relpath: {synced_hash, remote_sha}}

DAILY_FOLDER_NAME = 'Daily'

_lock = threading.Lock()   # guards config/state read-modify-write and sync itself
                             # (a single lock is fine here: sync is manual and rare,
                             # never something worth making concurrent)

GITHUB_API = 'https://api.github.com'


# ── Hidden metadata tag (same convention as the plain Notes module) ──
# Every LOCAL copy gets a trailing HTML comment recording its creation and
# last modification timestamps. This is purely local bookkeeping — it is
# stripped before hashing (see _local_hash_map) so it never falsely marks
# a note "unsynced" just because you reopened and it re-saved with a new
# timestamp, and it is stripped before ever being pushed to GitHub, so the
# repo itself only ever contains clean markdown.
_META_RE = re.compile(r'\n?<!--meta:created=(?P<created>[^|]*)\|modified=(?P<modified>.*?)-->\s*\Z', re.DOTALL)

_WIKILINK_RE = re.compile(r'\[\[(.+?)\]\]')

# ── Local-file links ─────────────────────────────────────────────────
# See the matching comment in notes.py — a browser cannot launch a
# desktop app for a local path, so any markdown link that looks like one
# is routed through /notes_sync/file, which sends it back as a download
# instead.
_LOCAL_PATH_RE = re.compile(r'^(?:[A-Za-z]:[\\/]|\\\\|file:///?)', re.IGNORECASE)
_MD_LINK_RE = re.compile(r'\[([^\[\]]+)\]\(([^()]+)\)')


def _convert_local_file_link(match):
    label, target = match.group(1), match.group(2).strip()
    if not _LOCAL_PATH_RE.match(target):
        return match.group(0)
    raw_path = target
    if raw_path.lower().startswith('file:///'):
        raw_path = urllib.parse.unquote(raw_path[8:])
    elif raw_path.lower().startswith('file://'):
        raw_path = urllib.parse.unquote(raw_path[7:])
    encoded = urllib.parse.quote(raw_path, safe='')
    return f'[{label}](localfile:{encoded})'


def _resolve_wikilink_target(raw_target, all_paths):
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


# ── Local vault file helpers ─────────────────────────────────────────

def _local_note_paths():
    paths = []
    for root, dirs, files in os.walk(VAULT_FOLDER):
        for file in files:
            if file.endswith('.md'):
                rel = os.path.relpath(os.path.join(root, file), VAULT_FOLDER).replace('\\', '/')
                paths.append(rel)
    return sorted(paths)


def _read_local_raw(rel):
    with open(os.path.join(VAULT_FOLDER, rel), 'r', encoding='utf-8') as f:
        return f.read()


def _write_local_raw(rel, raw_text):
    path = os.path.join(VAULT_FOLDER, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(raw_text)


def _delete_local_file(rel):
    path = os.path.join(VAULT_FOLDER, rel)
    if os.path.exists(path):
        os.remove(path)


def _content_hash(text):
    """Normalized hash used to detect real content changes — trailing
    whitespace differences alone don't count as "unsynced"."""
    return hashlib.sha256((text or '').strip().encode('utf-8')).hexdigest()


def _local_hash_map():
    """rel path -> hash of the BODY ONLY (meta comment stripped) for every
    local note. See the _META_RE comment above for why meta is excluded."""
    result = {}
    for rel in _local_note_paths():
        try:
            raw = _read_local_raw(rel)
        except Exception:
            continue
        result[rel] = _content_hash(_strip_meta(raw))
    return result


# ── Config / sync-state persistence (atomic write, same pattern used
#    throughout this app) ──────────────────────────────────────────────

# Set by _load_config() whenever CONFIG_FILE exists but isn't valid JSON
# (e.g. a missing comma from hand-editing it) — kept separate from "not
# configured yet", which just means the placeholder hasn't been filled
# in. Without this distinction both cases looked identical to the user:
# a syntax error silently produced an empty {} config, which reported the
# exact same generic "Not configured" message as a freshly-seeded,
# never-touched template.
_config_load_error = None


def _load_config():
    global _config_load_error
    _config_load_error = None
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            _config_load_error = f'{CONFIG_FILE} is not valid JSON: {e}'
            return {}
    return {}


def _save_config(cfg):
    tmp = CONFIG_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(cfg, f)
    os.replace(tmp, CONFIG_FILE)


def _ensure_config_template():
    """Writes a placeholder config.json the first time this module ever
    starts without one, so there's something concrete to open and edit —
    there is no in-app settings screen, this file is the only way to
    configure sync. Never overwrites an existing file."""
    if os.path.exists(CONFIG_FILE):
        return
    _save_config({
        'token': 'PASTE_YOUR_GITHUB_TOKEN_HERE',
        'repo': 'owner/repo-name',
        'branch': 'main',
        'subdir': '',
    })


_PLACEHOLDER_TOKEN = 'PASTE_YOUR_GITHUB_TOKEN_HERE'
_PLACEHOLDER_REPO = 'owner/repo-name'


def _is_configured(cfg):
    """True only once the placeholder template has actually been edited —
    a freshly-seeded config.json must never be reported as configured."""
    token = cfg.get('token', '')
    repo = cfg.get('repo', '')
    return bool(token) and token != _PLACEHOLDER_TOKEN and bool(repo) and repo != _PLACEHOLDER_REPO and '/' in repo


_ensure_config_template()


def _load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def _save_state(state):
    tmp = STATE_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(state, f)
    os.replace(tmp, STATE_FILE)


def _touch_last_synced():
    cfg = _load_config()
    cfg['last_synced_at'] = datetime.now().isoformat(timespec='seconds')
    _save_config(cfg)


def _compute_dirty(state=None, local_hashes=None):
    """Returns (dirty_paths, pending_deletes) — files edited locally since
    the last successful sync, and files that WERE synced but have since
    been deleted locally (and so are pending a delete-on-GitHub next
    push). Purely local computation — no network calls."""
    state = state if state is not None else _load_state()
    local_hashes = local_hashes if local_hashes is not None else _local_hash_map()
    dirty = [rel for rel, h in local_hashes.items()
             if (rel not in state) or (state[rel].get('synced_hash') != h)]
    pending_deletes = [rel for rel in state.keys() if rel not in local_hashes]
    return sorted(dirty), sorted(pending_deletes)


# ── GitHub REST API helpers ──────────────────────────────────────────

def _gh_headers(token):
    return {
        'Authorization': f'token {token}',
        'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
    }


def _gh_get_tree(owner, repo, branch, token):
    import requests
    url = f'{GITHUB_API}/repos/{owner}/{repo}/git/trees/{branch}'
    r = requests.get(url, headers=_gh_headers(token), params={'recursive': '1'}, timeout=30)
    if r.status_code == 404:
        raise RuntimeError(f'Repo or branch not found ({owner}/{repo}@{branch}). Check Settings.')
    if r.status_code == 401:
        raise RuntimeError('GitHub rejected the token (401 Unauthorized). Check Settings.')
    r.raise_for_status()
    data = r.json()
    if data.get('truncated'):
        raise RuntimeError('That repository tree is too large for one listing call — '
                            'this module expects a personal notes-sized repo.')
    return data.get('tree', [])


def _gh_get_blob(owner, repo, sha, token):
    import requests
    url = f'{GITHUB_API}/repos/{owner}/{repo}/git/blobs/{sha}'
    r = requests.get(url, headers=_gh_headers(token), timeout=30)
    r.raise_for_status()
    data = r.json()
    content = data.get('content', '')
    if data.get('encoding') == 'base64':
        return base64.b64decode(content).decode('utf-8')
    return content


def _gh_put_file(owner, repo, path, branch, token, text, message, sha=None):
    import requests
    url = f'{GITHUB_API}/repos/{owner}/{repo}/contents/{path}'
    body = {
        'message': message,
        'content': base64.b64encode(text.encode('utf-8')).decode('ascii'),
        'branch': branch,
    }
    if sha:
        body['sha'] = sha
    r = requests.put(url, headers=_gh_headers(token), json=body, timeout=30)
    if not r.ok:
        raise RuntimeError(f'GitHub rejected the update for "{path}": {r.status_code} {r.text[:200]}')
    return r.json()['content']['sha']


def _gh_delete_file(owner, repo, path, branch, token, sha, message):
    import requests
    url = f'{GITHUB_API}/repos/{owner}/{repo}/contents/{path}'
    body = {'message': message, 'sha': sha, 'branch': branch}
    r = requests.delete(url, headers=_gh_headers(token), json=body, timeout=30)
    if not r.ok:
        raise RuntimeError(f'GitHub rejected the delete for "{path}": {r.status_code} {r.text[:200]}')


def _write_pulled_file(rel, remote_text):
    """Writes remote content locally, preserving the local 'created'
    timestamp if this note already existed (so pulling an update doesn't
    make a note look newly-created)."""
    existing_created = None
    path = os.path.join(VAULT_FOLDER, rel)
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                existing_created, _ = _extract_meta(f.read())
        except Exception:
            pass
    now = datetime.now().isoformat(timespec='seconds')
    _write_local_raw(rel, _with_meta(remote_text, existing_created or now, now))


def _write_conflict_copy(rel, remote_text):
    """Writes the GitHub version alongside the local one as
    '<name>.conflict-<timestamp>.md' and returns that relative path."""
    base, ext = os.path.splitext(rel)
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    conflict_rel = f'{base}.conflict-{stamp}{ext or ".md"}'
    now = datetime.now().isoformat(timespec='seconds')
    _write_local_raw(conflict_rel, _with_meta(remote_text, now, now))
    return conflict_rel


# ── Core sync operation ───────────────────────────────────────────────

def _perform_sync():
    try:
        import requests  # noqa: F401 (import-checked here so the error message is friendly)
    except ImportError:
        return {'status': 'error', 'message': 'The "requests" package is not installed. '
                                               'Run: pip install requests --break-system-packages'}

    cfg = _load_config()
    if not _is_configured(cfg):
        if _config_load_error:
            return {'status': 'error', 'message': _config_load_error + ' — fix the syntax '
                                                   '(e.g. a missing comma between fields) and try again.'}
        return {'status': 'error', 'message': f'Not configured — edit "{CONFIG_FILE}" and fill in '
                                               f'your GitHub token and repo (owner/name), then try again.'}
    owner, repo = cfg['repo'].split('/', 1)
    token = cfg['token']
    branch = cfg.get('branch') or 'main'
    subdir = (cfg.get('subdir') or '').strip('/')
    prefix = subdir + '/' if subdir else ''

    try:
        tree = _gh_get_tree(owner, repo, branch, token)
    except Exception as e:
        return {'status': 'error', 'message': str(e)}

    remote_files = {}  # rel path (relative to subdir) -> blob sha
    for entry in tree:
        if entry.get('type') != 'blob':
            continue
        path = entry['path']
        if prefix and not path.startswith(prefix):
            continue
        rel = path[len(prefix):] if prefix else path
        if rel.endswith('.md'):
            remote_files[rel] = entry['sha']

    state = _load_state()
    local_hashes = _local_hash_map()

    pulled, conflicts, deleted_local = [], [], []

    # 1. Remote adds/updates
    for rel, remote_sha in remote_files.items():
        prior = state.get(rel)
        remote_changed = (not prior) or (prior.get('remote_sha') != remote_sha)
        if not remote_changed:
            continue

        local_exists = rel in local_hashes
        local_dirty = bool(prior) and local_exists and (local_hashes.get(rel) != prior.get('synced_hash'))

        try:
            remote_text = _gh_get_blob(owner, repo, remote_sha, token)
        except Exception as e:
            conflicts.append({'path': rel, 'note': f'Could not fetch from GitHub: {e}'})
            continue

        if local_exists and local_dirty:
            conflict_path = _write_conflict_copy(rel, remote_text)
            conflicts.append({'path': rel, 'note': f'Changed both locally and on GitHub — '
                                                    f'GitHub\'s version was saved as "{conflict_path}"'})
            continue

        _write_pulled_file(rel, remote_text)
        state[rel] = {'synced_hash': _content_hash(remote_text), 'remote_sha': remote_sha}
        pulled.append(rel)

    # 2. Remote deletions (tracked before, no longer present on GitHub)
    for rel in list(state.keys()):
        if rel in remote_files:
            continue
        prior = state[rel]
        local_exists = rel in local_hashes
        local_dirty = local_exists and (local_hashes.get(rel) != prior.get('synced_hash'))
        if local_exists and not local_dirty:
            _delete_local_file(rel)
            deleted_local.append(rel)
            state.pop(rel, None)
        elif not local_exists:
            state.pop(rel, None)
        else:
            # Deleted on GitHub, but you have local edits — keep the local
            # copy; dropping the state entry means the push step below
            # will treat it as a brand-new file and re-create it remotely.
            state.pop(rel, None)
            conflicts.append({'path': rel, 'note': 'Deleted on GitHub, but you have local edits — '
                                                     'it will be re-created on GitHub on the next sync'})

    _save_state(state)

    # Recompute after pull — pulled files are no longer "dirty" versus the
    # freshly-updated state, but anything untouched by the pull still is.
    local_hashes = _local_hash_map()
    conflicted_paths = {c['path'] for c in conflicts if 'path' in c}

    pushed, deleted_remote, errors = [], [], []

    # 3. Local adds/updates -> push
    for rel, h in local_hashes.items():
        if rel in conflicted_paths:
            continue
        prior = state.get(rel)
        if prior and prior.get('synced_hash') == h:
            continue
        try:
            body_text = _strip_meta(_read_local_raw(rel))
            sha = prior.get('remote_sha') if prior else None
            new_sha = _gh_put_file(owner, repo, prefix + rel, branch, token, body_text,
                                    message=f'Update {rel} via Notes (Sync)', sha=sha)
            state[rel] = {'synced_hash': h, 'remote_sha': new_sha}
            pushed.append(rel)
        except Exception as e:
            errors.append({'path': rel, 'error': str(e)})

    # 4. Local deletions -> push as delete
    for rel in list(state.keys()):
        if rel in local_hashes:
            continue
        prior = state[rel]
        try:
            _gh_delete_file(owner, repo, prefix + rel, branch, token, prior['remote_sha'],
                             message=f'Delete {rel} via Notes (Sync)')
            deleted_remote.append(rel)
            state.pop(rel, None)
        except Exception as e:
            errors.append({'path': rel, 'error': str(e)})

    _save_state(state)
    _touch_last_synced()

    return {
        'status': 'ok',
        'pulled': pulled,
        'pushed': pushed,
        'deleted_local': deleted_local,
        'deleted_remote': deleted_remote,
        'conflicts': conflicts,
        'errors': errors,
    }


# ── Page ─────────────────────────────────────────────────────────────

@bp.route('/notes_sync')
def notes_sync_module():
    note_files = _local_note_paths()
    state = _load_state()
    dirty, pending_deletes = _compute_dirty(state)
    cfg = _load_config()
    return render_template(
        'notes_sync.html',
        files=note_files,
        dirty_paths=dirty,
        pending_deletes=pending_deletes,
        configured=_is_configured(cfg),
        last_synced_at=cfg.get('last_synced_at'),
        config_path=CONFIG_FILE,
        config_error=_config_load_error,
    )


# ── Sync + status API ─────────────────────────────────────────────────

@bp.route('/notes_sync/status')
def status_route():
    cfg = _load_config()
    dirty, pending_deletes = _compute_dirty()
    return jsonify({
        'configured': _is_configured(cfg),
        'dirty_paths': dirty,
        'pending_deletes': pending_deletes,
        'dirty_count': len(dirty) + len(pending_deletes),
        'last_synced_at': cfg.get('last_synced_at'),
        'config_error': _config_load_error,
    })


@bp.route('/notes_sync/sync', methods=['POST'])
def sync_route():
    with _lock:
        result = _perform_sync()
    return jsonify(result)


# ── Notes API (same shape as the plain Notes module, pointed at the
#    Notes (Sync) vault instead) ──────────────────────────────────────

@bp.route('/notes_sync/load/<path:filename>')
def load_note(filename):
    path = os.path.join(VAULT_FOLDER, filename)
    with open(path, 'r', encoding='utf-8') as f:
        content = f.read()
    return jsonify({'content': _strip_meta(content)})


@bp.route('/notes_sync/save', methods=['POST'])
def save_note():
    data = request.json
    path = os.path.join(VAULT_FOLDER, data['filename'])
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


@bp.route('/notes_sync/delete', methods=['POST'])
def delete_note():
    path = os.path.join(VAULT_FOLDER, request.json['filename'])
    if os.path.exists(path):
        os.remove(path)
    return jsonify({'status': 'ok'})


def _daily_template(date_str):
    pretty = datetime.strptime(date_str, '%Y-%m-%d').strftime('%A, %d %B %Y')
    return f'# {pretty}\n\n## Tasks\n- [ ] \n\n## Daily Notes\n\n'


@bp.route('/notes_sync/daily', methods=['POST'])
def open_daily_note():
    date_str = datetime.now().strftime('%Y-%m-%d')
    rel_path = f'{DAILY_FOLDER_NAME}/{date_str}.md'
    full_path = os.path.join(VAULT_FOLDER, DAILY_FOLDER_NAME, f'{date_str}.md')

    created = False
    if not os.path.exists(full_path):
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        now = datetime.now().isoformat(timespec='seconds')
        content = _with_meta(_daily_template(date_str), now, now)
        with open(full_path, 'w', encoding='utf-8') as f:
            f.write(content)
        created = True

    return jsonify({'path': rel_path, 'created': created})


@bp.route('/notes_sync/search')
def search_notes():
    query = request.args.get('q', '').strip().lower()
    date_from = request.args.get('from', '').strip()
    date_to = request.args.get('to', '').strip()

    if not query and not date_from and not date_to:
        return jsonify({'matches': []})

    matches = []
    for root, _dirs, filenames in os.walk(VAULT_FOLDER):
        for filename in filenames:
            if not filename.endswith('.md'):
                continue
            full_path = os.path.join(root, filename)
            rel = os.path.relpath(full_path, VAULT_FOLDER).replace('\\', '/')

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


@bp.route('/notes_sync/meta')
def notes_meta():
    result = []
    for root, _dirs, filenames in os.walk(VAULT_FOLDER):
        for filename in filenames:
            if not filename.endswith('.md'):
                continue
            full_path = os.path.join(root, filename)
            rel = os.path.relpath(full_path, VAULT_FOLDER).replace('\\', '/')
            try:
                with open(full_path, 'r', encoding='utf-8') as f:
                    content = f.read()
            except Exception:
                continue
            created, modified = _extract_meta(content)
            result.append({'path': rel, 'created': created, 'modified': modified})
    return jsonify({'notes': result})


@bp.route('/notes_sync/file')
def serve_local_file():
    """See notes.py's serve_local_file — same idea, backs this vault's
    localfile: links."""
    path = request.args.get('path', '')
    if not path or not os.path.isfile(path):
        return 'File not found.', 404
    directory = os.path.dirname(path) or '.'
    filename = os.path.basename(path)
    return send_from_directory(directory, filename, as_attachment=True)


@bp.route('/notes_sync/graph')
def notes_graph():
    note_files = _local_note_paths()
    nodes, links, seen = [], [], set()

    for path in note_files:
        full_path = os.path.join(VAULT_FOLDER, path)
        try:
            with open(full_path, 'r', encoding='utf-8') as f:
                content = _strip_meta(f.read())
        except Exception:
            content = ''

        folder = path.split('/')[0] if '/' in path else ''
        label = path.split('/')[-1][:-3]
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


def _add_hard_breaks(lines):
    """Turns every single line break inside an admonition body into a real
    <br> (via markdown's two-trailing-spaces hard-break syntax) instead of
    being silently folded into the previous line — this is what
    previously made a callout need a blank line between every line to
    show a visible break. A blank line (an intentional paragraph
    separator within the callout) is left alone."""
    out = []
    n = len(lines)
    for i, line in enumerate(lines):
        stripped = line.rstrip()
        if stripped == '':
            out.append(line)
            continue
        next_blank = (i + 1 >= n) or (lines[i + 1].strip() == '')
        out.append(stripped if next_blank else stripped + '  ')
    return out


def _extract_admonitions(content, extract_other):
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
            processed = [block[0]] + _add_hard_breaks(block[1:])
            out.append(extract_other('\n'.join(processed)))
        else:
            out.append(line)
            i += 1
    return '\n'.join(out)


def _convert_wiki_link(match):
    inner = match.group(1).strip()
    if '|' in inner:
        target, label = inner.split('|', 1)
    else:
        target, label = inner, inner
    return f'[{label.strip()}](note:{target.strip()})'


@bp.route('/notes_sync/preview', methods=['POST'])
def preview_note():
    content = request.json.get('content', '')

    code_blocks = []

    def extract_code(match):
        code_blocks.append(match.group(0))
        return f"__MAGIC_CODE_BLOCK_{len(code_blocks) - 1}__"

    content = re.sub(r'```[\s\S]*?```', extract_code, content)

    # Protect LaTeX math the same way as the plain Notes module — see the
    # matching comment in notes.py. \( ... \) inline, $$ ... $$ or
    # \[ ... \] display; deliberately not bare single $...$ since that
    # clashes with plain dollar amounts.
    # Plain "__word__"-shaped placeholders get silently mangled here:
    # markdown's own bold/italic parser treats the leading/trailing "__"
    # as emphasis markup and strips it, so the literal token no longer
    # exists by the time we try to swap the real LaTeX back in below.
    # Private-Use-Area characters have no markdown meaning at all, so
    # they pass through untouched.
    _MATH_OPEN, _MATH_CLOSE = '\uE000', '\uE001'
    math_blocks = []

    def extract_math(match):
        math_blocks.append(match.group(0))
        return f"{_MATH_OPEN}{len(math_blocks) - 1}{_MATH_CLOSE}"

    content = re.sub(r'\$\$[\s\S]+?\$\$', extract_math, content)
    content = re.sub(r'\\\[[\s\S]+?\\\]', extract_math, content)
    content = re.sub(r'\\\([\s\S]+?\\\)', extract_math, content)

    content = re.sub(r'^#\+ +(.+)$', r'# \1', content, flags=re.MULTILINE)
    content = re.sub(r'^#- +(.+)$', r'# \1 {: .collapsed}', content, flags=re.MULTILINE)

    inline_code = []

    def extract_inline_code(match):
        inline_code.append(match.group(0))
        return f"__MAGIC_INLINE_CODE_{len(inline_code) - 1}__"

    content = re.sub(r'`[^`\n]+`', extract_inline_code, content)
    content = re.sub(r'\[\[(.+?)\]\]', _convert_wiki_link, content)
    content = _MD_LINK_RE.sub(_convert_local_file_link, content)
    for i, code in enumerate(inline_code):
        content = content.replace(f"__MAGIC_INLINE_CODE_{i}__", code)

    other_blocks = []

    def extract_other(text):
        other_blocks.append(text)
        return f"__MAGIC_OTHER_BLOCK_{len(other_blocks) - 1}__"

    def extract_other_match(match):
        return extract_other(match.group(0))

    content = re.sub(r'^\|.*\|(?:\n^\|.*\|)*', extract_other_match, content, flags=re.MULTILINE)
    content = _extract_admonitions(content, extract_other)
    content = re.sub(r'^[-*+] +\[[ xX]\].*(?:\n^[-*+] +\[[ xX]\].*)*', extract_other_match, content, flags=re.MULTILINE)

    content = re.sub(r'\n+', lambda m: '\n\n' + ('<br>' * (len(m.group(0)) - 1)) + '\n\n', content)

    for i, block in enumerate(other_blocks):
        content = content.replace(f"__MAGIC_OTHER_BLOCK_{i}__", f"\n\n{block}\n\n")

    for i, code in enumerate(code_blocks):
        content = content.replace(f"__MAGIC_CODE_BLOCK_{i}__", f"\n\n{code}\n\n")

    extensions = ['fenced_code', 'tables', 'extra', 'toc', ChecklistExtension(), 'admonition', 'attr_list']
    extension_configs = {'toc': {'toc_depth': 1}}
    html = markdown.markdown(content, extensions=extensions, extension_configs=extension_configs)

    html = re.sub(
        r'(<a\s+href="https?://[^"]*")',
        r'\1 target="_blank" rel="noopener noreferrer"',
        html,
    )

    for i, block in enumerate(math_blocks):
        html = html.replace(f"{_MATH_OPEN}{i}{_MATH_CLOSE}", _html_escape(block))

    return jsonify({'html': html})
