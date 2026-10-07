export const meta = { title: 'Private Notes' };

/* ─────────────────────────────────────────────────────────────────────────
   Private Notes — browser-only port of the Python "Notes (Sync)" module.

   Model (same as the Python version, minus the server):
   - Notes live in IndexedDB (via App.Store, key 'pn_vault'): instant open, instant
     search, instant graph, works offline. Edits autosave locally.
   - GitHub is touched ONLY when you press the sync button: one Trees call to list
     the repo, one Blob call per changed note, one Contents call per push/delete.
   - A note changed both locally and on GitHub is never overwritten: the GitHub
     version is saved next to yours as "<name>.conflict-<timestamp>.md".
   - Credentials come from the existing Settings modal (repo, token, folder).
   ───────────────────────────────────────────────────────────────────────── */

const PN_SUBDIR = '';   // optional extra sub-folder inside the Settings folder, e.g. 'private'
const DAILY = 'Daily';
const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];
const CDN = {
    marked: 'https://cdn.jsdelivr.net/npm/marked@15/marked.min.js',
    d3: 'https://cdnjs.cloudflare.com/ajax/libs/d3/7.9.0/d3.min.js',
    mathjax: 'https://cdn.jsdelivr.net/npm/mathjax@4/tex-chtml.js'
};

// ── Small utilities ──────────────────────────────────────────────────────
const scripts = {};
const loadScript = src => scripts[src] ??= new Promise((ok, fail) => {
    const s = document.createElement('script');
    s.src = src; s.onload = ok;
    s.onerror = () => { delete scripts[src]; fail(new Error('Could not load ' + src)); };
    document.head.appendChild(s);
});

const esc = s => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
const escRe = s => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
const pad = n => String(n).padStart(2, '0');
const isoDate = d => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const nowStr = () => { const d = new Date(); return `${isoDate(d)}T${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`; };
const strToB64 = s => { let b = ''; new TextEncoder().encode(s).forEach(c => b += String.fromCharCode(c)); return btoa(b); };
const b64ToStr = b => new TextDecoder().decode(Uint8Array.from(atob(b.replace(/\s/g, '')), c => c.charCodeAt(0)));
const encPath = p => p.split('/').map(encodeURIComponent).join('/');

// Fast non-crypto hash — only used to detect "did this note change since last sync".
function hash(s) {
    s = (s || '').trim();
    let h1 = 0xdeadbeef, h2 = 0x41c6ce57;
    for (let i = 0; i < s.length; i++) {
        const c = s.charCodeAt(i);
        h1 = Math.imul(h1 ^ c, 2654435761); h2 = Math.imul(h2 ^ c, 1597334677);
    }
    h1 = Math.imul(h1 ^ (h1 >>> 16), 2246822507) ^ Math.imul(h2 ^ (h2 >>> 13), 3266489909);
    h2 = Math.imul(h2 ^ (h2 >>> 16), 2246822507) ^ Math.imul(h1 ^ (h1 >>> 13), 3266489909);
    return (4294967296 * (2097151 & h2) + (h1 >>> 0)).toString(36);
}

// [[target]] / [[target|label]] → note path (exact → case-insensitive → filename only)
function resolveNote(raw, paths) {
    const t = raw.split('|')[0].trim();
    if (!t) return null;
    const c = /\.md$/i.test(t) ? t : t + '.md', lc = c.toLowerCase(), base = lc.split('/').pop();
    return paths.find(p => p === c)
        || paths.find(p => p.toLowerCase() === lc)
        || paths.find(p => p.split('/').pop().toLowerCase() === base)
        || null;
}

// ── Markdown → HTML (port of the Python /preview route) ──────────────────
// Placeholders use Private-Use-Area characters: markdown has no meaning for them,
// so they survive parsing untouched (plain "__X__" tokens get eaten as bold).
const T = { math: ['\uE000', '\uE001'], fence: ['\uE002', '\uE003'], block: ['\uE004', '\uE005'], html: ['\uE006', '\uE007'], code: ['\uE009', '\uE00A'] };
const COLLAPSED = '\uE008';
const stasher = (list, [a, b]) => s => { list.push(s); return a + (list.length - 1) + b; };
const unstash = (text, list, [a, b], fn = s => s) => text.replace(new RegExp(a + '(\\d+)' + b, 'g'), (_, i) => fn(list[i]));

function renderMd(src) {
    if (!window.marked) return `<pre>${esc(src)}</pre>`;
    const fences = [], maths = [], blocks = [], htmls = [], codes = [];
    const sFence = stasher(fences, T.fence), sMath = stasher(maths, T.math), sBlock = stasher(blocks, T.block),
          sHtml = stasher(htmls, T.html), sCode = stasher(codes, T.code);

    let c = src.replace(/```[\s\S]*?```/g, sFence)
        .replace(/\$\$[\s\S]+?\$\$|\\\[[\s\S]+?\\\]|\\\([\s\S]+?\\\)/g, sMath)   // LaTeX (no bare $...$ — clashes with money)
        .replace(/^#\+ +(.+)$/gm, '# $1')                                       // "#+ H" expanded section
        .replace(/^#- +(.+)$/gm, `# $1${COLLAPSED}`);                           // "#- H" starts collapsed

    // wiki links (inline code is protected while converting)
    c = c.replace(/`[^`\n]+`/g, sCode)
        .replace(/\[\[(.+?)\]\]/g, (_, i) => {
            const [t, l] = i.split('|');
            return `[${(l ?? t).trim()}](note:${encodeURIComponent(t.trim())})`;
        });
    c = unstash(c, codes, T.code);

    // tables, !!! admonitions, checklists are kept out of the newline-spacing step below
    c = c.replace(/^\|.*\|(?:\n^\|.*\|)*/gm, sBlock);
    c = admonitions(c, (type, title, body) => {
        const inner = window.marked.parse(unstash(body, fences, T.fence), { gfm: true, breaks: true });
        return sHtml(`<div class="admonition ${esc(type)}">${title ? `<p class="admonition-title">${esc(title)}</p>` : ''}${inner}</div>`);
    });
    c = c.replace(/^[-*+] +\[[ xX]\].*(?:\n^[-*+] +\[[ xX]\].*)*/gm, sBlock);

    // Every line break is a paragraph; each extra blank line becomes one visible <br> (same look as the Python app)
    c = c.replace(/\n+/g, m => '\n\n' + '<br>'.repeat(m.length - 1) + '\n\n');

    c = unstash(c, blocks, T.block, b => `\n\n${b}\n\n`);
    c = unstash(c, fences, T.fence, b => `\n\n${b}\n\n`);

    let html = window.marked.parse(c, { gfm: true });
    html = html.replace(new RegExp(`<p>(${T.html[0]}\\d+${T.html[1]})</p>`, 'g'), '$1');
    html = unstash(html, htmls, T.html);
    return unstash(html, maths, T.math, esc);
}

function admonitions(text, emit) {
    const lines = text.split('\n'), out = [], indented = /^(?: {4,}|\t)/;
    let i = 0;
    while (i < lines.length) {
        const m = lines[i].match(/^!!!\s+(\w+)(?:\s+"(.*)")?\s*$/);
        if (!m) { out.push(lines[i++]); continue; }
        i++;
        const body = [];
        while (i < lines.length) {
            if (indented.test(lines[i])) body.push(lines[i++].replace(/^(?: {4}|\t)/, ''));
            else if (!lines[i].trim()) {
                let j = i;
                while (j < lines.length && !lines[j].trim()) j++;
                if (j < lines.length && indented.test(lines[j])) { while (i < j) { body.push(''); i++; } }
                else break;
            } else break;
        }
        const type = m[1].toLowerCase();
        out.push(emit(type, m[2] ?? type[0].toUpperCase() + type.slice(1), body.join('\n')));
    }
    return out.join('\n');
}

// ── Styles (scoped under .pn so nothing leaks into other modules) ────────
const CSS = `
.pn{height:100%}
.pn .pn-search{display:flex;gap:6px;padding:10px 10px 0;flex-shrink:0}
.pn .pn-search .input{flex:1;min-width:0;height:32px;font-size:.75rem;padding:0 8px}
.pn .pn-ibtn{position:relative;width:32px;height:32px;flex-shrink:0;display:flex;align-items:center;justify-content:center;border:1px solid var(--border);border-radius:6px;background:var(--surface-card);color:var(--text-muted);cursor:pointer;transition:border-color 130ms,color 130ms,background 130ms}
.pn .pn-ibtn:hover{border-color:var(--teal-700);color:var(--teal-700);background:var(--surface-in)}
.pn .pn-ibtn.active{border-color:var(--teal-700);color:var(--teal-700);background:var(--teal-100)}
.pn .pn-ibtn.spin svg{animation:pn-spin .9s linear infinite}
@keyframes pn-spin{to{transform:rotate(360deg)}}
.pn .pn-badge{position:absolute;top:-5px;right:-5px;display:none;align-items:center;justify-content:center;min-width:15px;height:15px;padding:0 3px;border-radius:8px;background:var(--amber);color:var(--ink);font-size:.5625rem;font-weight:700;line-height:1}
.pn .pn-empty{color:var(--text-muted);font-size:.75rem;padding:10px 4px}
.pn mark{background:var(--amber);color:var(--ink);border-radius:2px;padding:0 1px}
.pn .pn-actions{display:flex;gap:8px;padding:10px}
.pn .pn-dashed{flex:1;display:flex;align-items:center;justify-content:center;gap:7px;padding:8px;border:1px dashed var(--teal-700);background:none;color:var(--text-muted);cursor:pointer;border-radius:6px;font-size:.6875rem;font-weight:600;font-family:var(--font);text-transform:uppercase;letter-spacing:.04em;transition:background 130ms,color 130ms}
.pn .pn-dashed:hover{background:var(--surface-in);color:var(--teal-700)}
.pn .pn-dashed svg{width:15px;height:15px}
.pn .pn-graphbar{border-top:1px solid var(--border);padding:0 10px 10px;padding-top:10px;flex-shrink:0;display:flex}
.pn .folder-chevron{width:18px;height:18px;color:var(--teal-700)}
.pn .folder-chevron svg{width:18px;height:18px}
.pn .folder-header{gap:4px}
.pn .folder-content{margin-left:16px;padding-left:12px;border-left:3px solid var(--teal-100)}
.pn .file-item.conflict{color:var(--amber)}
.pn .file-item.unsynced::after{content:'';display:inline-block;width:6px;height:6px;border-radius:50%;background:var(--amber);margin-left:6px;vertical-align:middle}

.pn .editor-textarea:hover{border-color:var(--teal-700)}
.pn .editor-textarea:focus{border-color:var(--teal-700);box-shadow:0 0 0 3px rgba(0,130,130,.15)}
.pn .pn-banner{display:flex;align-items:center;gap:16px;padding:12px 16px;background:var(--surface-card);border:1px solid var(--border);border-radius:var(--r);box-shadow:var(--shadow);flex-shrink:0}
.pn .pn-banner-main{flex:1;min-width:0}
.pn .pn-title-view{display:block;font-size:1.25rem;font-weight:700;color:var(--teal-900);line-height:1.3;cursor:text;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;border-radius:4px;padding:0 4px;margin:0 -4px;transition:background 130ms}
.pn .pn-title-view:hover:not(.static){background:var(--surface-in)}
.pn .pn-title-view.placeholder{color:var(--text-muted);font-weight:500;font-style:italic}
.pn .pn-title-view.static{cursor:default}
.pn .pn-title-input{width:100%;height:34px;font-family:var(--font);font-size:1.0625rem;font-weight:600;color:var(--teal-900);background:var(--surface-in);border:1px solid var(--teal-700);border-radius:6px;padding:0 10px;outline:none;box-shadow:0 0 0 3px rgba(0,130,130,.15)}
.pn .pn-meta{font-size:.6875rem;color:var(--text-muted);margin-top:2px}
.pn .pn-meta:empty{display:none}
.pn .pn-status{white-space:nowrap;font-size:.625rem;color:var(--teal-400);font-weight:600;text-transform:uppercase;letter-spacing:.06em}
.pn .pn-banner-actions{display:flex;align-items:center;gap:10px;flex-shrink:0}
.pn .pn-seg{display:inline-flex;border:1px solid var(--teal-900);border-radius:6px;overflow:hidden;height:30px}
.pn .pn-seg button{border:0;background:transparent;color:var(--teal-900);font-family:var(--font);font-size:.75rem;font-weight:600;padding:0 14px;cursor:pointer;transition:background 130ms,color 130ms}
.pn .pn-seg button+button{border-left:1px solid var(--teal-900)}
.pn .pn-seg button.on{background:var(--teal-900);color:#fff}
.pn .pn-seg button:not(.on):hover{background:rgba(0,90,90,.08)}
.pn .pn-icon-btn{width:30px;height:30px;display:inline-flex;align-items:center;justify-content:center;border:1px solid var(--border);border-radius:6px;background:var(--surface-card);color:var(--text-muted);cursor:pointer;transition:background 130ms,border-color 130ms,color 130ms}
.pn .pn-icon-btn.danger:hover{background:var(--red);border-color:var(--red);color:#fff}
@media (max-width:768px){.pn .pn-banner{flex-wrap:wrap}}
.pn .pn-hint{color:var(--text-muted);font-size:.8125rem}

.pn .pn-toast{position:fixed;bottom:22px;right:22px;background:var(--teal-900);color:#fff;font-size:.8125rem;font-weight:600;padding:11px 18px;border-radius:7px;box-shadow:var(--shadow-md);opacity:0;transform:translateY(8px);transition:opacity 180ms,transform 180ms;pointer-events:none;z-index:2000;max-width:min(420px,90vw)}
.pn .pn-toast.show{opacity:1;transform:none}
.pn .pn-toast.error{background:var(--red)}

/* Markdown preview */
.pn .preview-area p{margin:0;min-height:1em}
.pn .preview-area h1,.pn .preview-area h2,.pn .preview-area h3{color:var(--text);font-weight:600;margin:0}
.pn .preview-area h1{font-size:1.375rem}
.pn .preview-area h2{font-size:1.125rem;padding-bottom:7px;border-bottom:2px solid var(--teal-100)}
.pn .preview-area h3{font-size:.9375rem}
.pn .h1-section+.h1-section{margin-top:22px;padding-top:22px;border-top:1px solid var(--border)}
.pn .h1-header{display:flex;align-items:center;gap:10px;cursor:pointer;user-select:none;padding:4px 6px;margin:-4px -6px;border-radius:6px;transition:background 130ms}
.pn .h1-header:hover{background:var(--surface-in)}
.pn .h1-header h1{flex:1}
.pn .h1-chevron{width:20px;height:20px;flex-shrink:0;color:var(--teal-400);transition:transform 200ms}
.pn .h1-section:not(.expanded) .h1-chevron{transform:rotate(-90deg)}
.pn .h1-section:not(.expanded) .h1-body{display:none}
.pn .preview-area ul,.pn .preview-area ol{margin:0;padding-left:2em}
.pn .preview-area li{margin:.25em 0}
.pn .preview-area ul:not(:has(input[type=checkbox])){list-style:none;padding-left:1.4em}
.pn .preview-area ul:not(:has(input[type=checkbox]))>li{position:relative}
.pn .preview-area ul:not(:has(input[type=checkbox]))>li::before{content:'';position:absolute;left:-1.15em;top:.55em;width:8px;height:8px;border-radius:2px;background:var(--teal-700)}
.pn .preview-area hr{border:none;height:2px;background:var(--border);margin:0;border-radius:2px}
.pn .preview-area blockquote{margin:0;padding:10px 16px;border-left:4px solid var(--amber);background:var(--surface-in);font-style:italic;color:var(--text-muted)}
.pn .preview-area .toc{background:var(--surface-in);border:1px solid var(--border);border-left:4px solid var(--teal-700);box-shadow:var(--shadow);padding:16px;display:inline-block;min-width:240px}
.pn .preview-area .toc .toctitle{display:block;font-weight:700;text-transform:uppercase;color:var(--text-muted);margin-bottom:8px;font-size:.6875rem}
.pn .preview-area .toc ul{list-style:none;padding-left:0}
.pn .preview-area .toc ul>li::before{display:none}
.pn .preview-area a{color:var(--teal-700);text-decoration:underline}
.pn .preview-area a:hover{color:var(--teal-900)}
.pn .preview-area a.note-link{text-decoration:none;border-bottom:1px dashed var(--teal-400);cursor:pointer}
.pn .preview-area a.note-link:hover{color:var(--teal-900);border-bottom-color:var(--teal-900)}
.pn .preview-area a.note-missing{border-bottom-color:var(--amber)}
.pn .admonition{padding:16px;border-left:4px solid var(--teal-700);background:var(--surface-in);box-shadow:var(--shadow);color:var(--ink)}
.pn .admonition p{margin:0 0 10px}
.pn .admonition p:last-child{margin-bottom:0}
.pn .admonition-title{font-weight:700;font-size:.75rem;text-transform:uppercase;margin-bottom:8px;display:block;color:var(--teal-700)}
.pn .admonition.warning{border-left-color:#d97706;background:rgba(217,119,6,.05);color:#7a4a06}
.pn .admonition.warning .admonition-title{color:#d97706}
.pn .admonition.danger,.pn .admonition.error{border-left-color:#e53e3e;background:rgba(229,62,62,.05);color:#8a2424}
.pn .admonition.danger .admonition-title,.pn .admonition.error .admonition-title{color:#e53e3e}
.pn .preview-area ul:has(input[type=checkbox]){list-style:none;padding-left:.5rem}
.pn .preview-area li:has(input[type=checkbox]){list-style:none;display:flex;align-items:flex-start;gap:8px}
.pn .preview-area input[type=checkbox]{appearance:none;-webkit-appearance:none;width:16px;height:16px;margin-top:4px;flex-shrink:0;border:1.5px solid var(--teal-700);border-radius:4px;background:var(--surface-card);cursor:pointer;position:relative;transition:background 130ms,border-color 130ms}
.pn .preview-area input[type=checkbox]:hover{border-color:var(--teal-900)}
.pn .preview-area input[type=checkbox]:checked{background:var(--teal-700)}
.pn .preview-area input[type=checkbox]:checked::after{content:'';position:absolute;left:4px;top:1px;width:4px;height:8px;border:solid #fff;border-width:0 2px 2px 0;transform:rotate(45deg)}
.pn .preview-area li:has(>input[type=checkbox]:checked){color:var(--text-muted)}
.pn .preview-area table{width:fit-content;max-width:100%;font-size:.8125rem;background:var(--surface-card);border-collapse:separate;border-spacing:0;border:1px solid var(--border);box-shadow:var(--shadow)}
.pn .preview-area th{background:var(--teal-900);color:#fff;font-weight:600;text-align:left;padding:10px 16px;border-right:1px solid var(--teal-700);font-size:.75rem}
.pn .preview-area td{padding:10px 16px;border-right:1px solid var(--border);border-bottom:1px solid var(--border)}
.pn .preview-area th:last-child,.pn .preview-area td:last-child{border-right:none}
.pn .preview-area tr:last-child td{border-bottom:none}
.pn .preview-area tbody tr:hover td{background:var(--surface-in)}
.pn .preview-area pre{position:relative;padding:16px;background:var(--surface-in);border:1px solid var(--border);border-radius:8px;overflow-x:auto;white-space:pre-wrap}
.pn .preview-area code{font-family:monospace;font-size:.8em}
.pn .preview-area pre code{background:none;padding:0}
.pn .preview-area mjx-container[display=true]{text-align:center}
.pn .copy-code-btn{position:absolute;top:8px;right:8px;border:1px solid var(--border);background:var(--surface-card);color:var(--text-muted);padding:4px 10px;border-radius:4px;cursor:pointer;font-size:.6875rem;font-weight:600;font-family:var(--font)}
.pn .copy-code-btn:hover{background:var(--surface-in);color:var(--text);border-color:var(--teal-700)}

/* Modals: calendar + graph */
.pn .btn-sm{height:28px;padding:0 10px;font-size:.75rem}
.pn .pn-overlay{display:none;position:fixed;inset:0;z-index:1000;background:rgba(1,45,44,.32);align-items:center;justify-content:center}
.pn .pn-overlay.open{display:flex}
.pn .cal-modal{background:var(--surface-card);border-radius:12px;width:300px;max-width:92vw;box-shadow:var(--shadow-md);border:1px solid var(--border)}
.pn .cal-head{display:flex;align-items:flex-start;padding:14px 16px;border-bottom:1px solid var(--border);gap:10px}
.pn .cal-title{font-weight:600;font-size:.8125rem;color:var(--text)}
.pn .cal-sub{font-size:.625rem;color:var(--text-muted);margin-top:2px}
.pn .cal-body{padding:14px 16px}
.pn .cal-foot{padding:10px 16px;border-top:1px solid var(--border);display:flex;justify-content:flex-end;gap:8px}
.pn .cal-nav{display:flex;align-items:center;justify-content:space-between;margin-bottom:10px}
.pn .cal-month{font-size:.75rem;font-weight:600;color:var(--text)}
.pn .cal-grid{display:grid;grid-template-columns:repeat(7,1fr);gap:4px}
.pn .cal-dow{text-align:center;font-size:.5625rem;font-weight:700;color:var(--text-muted);text-transform:uppercase;padding-bottom:4px}
.pn .cal-day{position:relative;aspect-ratio:1;display:flex;align-items:center;justify-content:center;border-radius:6px;font-size:.6875rem;color:var(--text);cursor:pointer;user-select:none;background:var(--surface-in);transition:background 130ms,color 130ms}
.pn .cal-day:hover{background:var(--teal-100)}
.pn .cal-day.empty{visibility:hidden;cursor:default}
.pn .cal-day.today{box-shadow:inset 0 0 0 1.5px var(--teal-700)}
.pn .cal-day.in-range{background:var(--teal-100)}
.pn .cal-day.edge{background:var(--teal-700);color:#fff;font-weight:700}
.pn .cal-count{position:absolute;top:-4px;right:-4px;min-width:14px;height:14px;padding:0 3px;border-radius:7px;background:var(--amber);color:var(--ink);font-size:.5rem;font-weight:700;line-height:1;display:flex;align-items:center;justify-content:center}
.pn .graph-shell{position:relative;width:94vw;height:90vh;background:var(--surface-card);border-radius:12px;border:1px solid var(--border);box-shadow:var(--shadow-md);overflow:hidden}
.pn .graph-wrap{position:relative;width:100%;height:100%;background:var(--surface-in);cursor:grab}
.pn .graph-wrap:active{cursor:grabbing}
.pn .graph-wrap svg{width:100%;height:100%;display:block}
.pn .graph-close{position:absolute;top:14px;right:14px;z-index:5;width:34px;height:34px;padding:0;border-radius:50%;background:var(--surface-card)}
.pn .graph-empty{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;color:var(--text-muted);font-size:.75rem;text-align:center;padding:10px}
.pn .g-link{stroke-width:.6px;transition:opacity 100ms}
.pn .g-link.dim{opacity:.08!important}
.pn .g-link.hi{stroke-width:1.4px}
.pn .g-node{stroke:var(--teal-900);stroke-width:.6px;cursor:pointer;transition:stroke-width 100ms,opacity 100ms}
.pn .g-node.dim{opacity:.2}
.pn .g-node:hover{stroke-width:1.6px}
.pn .g-node.nb{stroke-width:1.5px;filter:drop-shadow(0 0 1.5px var(--teal-900))}
.pn .g-label{font-family:var(--font);font-size:9px;fill:var(--teal-700);pointer-events:none;opacity:0;transition:opacity 100ms;paint-order:stroke fill;stroke:var(--surface-in);stroke-width:3px;stroke-linejoin:round}
.pn .g-label.nb{opacity:1;font-weight:600}
.pn .g-label.show{opacity:1;font-size:11px;font-weight:700;fill:var(--teal-900)}
.pn .g-label.dim{opacity:0}
`;

const TEMPLATE = `
<div class="pn">
  <div class="notes-layout">
    <div class="file-panel">
      <div class="pn-search">
        <input type="text" id="pn-search" class="input" placeholder="Search notes…">
        <button type="button" class="pn-ibtn" id="pn-cal-btn" title="Filter by date">
          <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="18" rx="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/></svg>
        </button>
        <button type="button" class="pn-ibtn" id="pn-sync-btn" title="Sync with GitHub">
          <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg>
          <span class="pn-badge" id="pn-badge"></span>
        </button>
      </div>
      <div class="file-list" id="pn-tree"></div>
      <div class="pn-actions">
        <button class="pn-dashed" id="pn-new-btn">+ New note</button>
        <button class="pn-dashed" id="pn-daily-btn">Daily note</button>
      </div>
      <div class="pn-graphbar">
        <button class="pn-dashed" id="pn-graph-btn">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="6" height="6" rx="1"/><rect x="15" y="3" width="6" height="6" rx="1"/><rect x="9" y="15" width="6" height="6" rx="1"/><line x1="9" y1="6" x2="15" y2="6"/><line x1="6" y1="9" x2="10" y2="16"/><line x1="18" y1="9" x2="14" y2="16"/></svg>
          Graph view
        </button>
      </div>
    </div>

    <div class="editor-panel">
      <div class="pn-banner">
        <div class="pn-banner-main">
          <span id="pn-title-view" class="pn-title-view" title="Click to rename"></span>
          <input type="text" id="pn-title" class="pn-title-input" style="display:none" spellcheck="false" placeholder="Folder/Subfolder/note name">
          <div class="pn-meta" id="pn-modified"></div>
        </div>
        <div class="pn-banner-actions" id="pn-controls" style="display:none">
          <span class="pn-status" id="pn-status"></span>
          <button class="btn btn-primary btn-sm" id="pn-save-btn" style="display:none">Save</button>
          <div class="pn-seg" role="group" aria-label="Mode">
            <button type="button" id="pn-seg-preview" class="on">Preview</button><button type="button" id="pn-seg-edit">Edit</button>
          </div>
          <button type="button" class="pn-icon-btn danger" id="pn-del-btn" title="Delete note" style="display:none">
            <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6M14 11v6"/><path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/></svg>
          </button>
        </div>
      </div>
      <textarea id="pn-editor" class="editor-textarea" style="display:none" placeholder="# Start writing…&#10;&#10;#+ Heading = expanded section, #- Heading = starts collapsed.&#10;[[Note name]] links notes. !!! warning &quot;Title&quot; + indented lines = callout."></textarea>
      <div id="pn-preview" class="preview-area" style="display:block"><div class="pn-hint">Select a note on the left, or create a new one.</div></div>
    </div>
  </div>

  <div class="pn-toast" id="pn-toast"></div>

  <div class="pn-overlay" id="pn-cal-overlay">
    <div class="cal-modal">
      <div class="cal-head">
        <div style="flex:1"><div class="cal-title">Filter by date</div><div class="cal-sub" id="pn-cal-label"></div></div>
        <button class="btn btn-outline btn-sm" id="pn-cal-x">&#x2715;</button>
      </div>
      <div class="cal-body">
        <div class="cal-nav">
          <button class="btn btn-outline btn-sm" id="pn-cal-prev">&#8249;</button>
          <span class="cal-month" id="pn-cal-month"></span>
          <button class="btn btn-outline btn-sm" id="pn-cal-next">&#8250;</button>
        </div>
        <div class="cal-grid" id="pn-cal-grid"></div>
      </div>
      <div class="cal-foot">
        <button class="btn btn-outline btn-sm" id="pn-cal-clear">Clear</button>
        <button class="btn btn-primary btn-sm" id="pn-cal-apply">Apply</button>
      </div>
    </div>
  </div>

  <div class="pn-overlay" id="pn-graph-overlay">
    <div class="graph-shell">
      <button class="btn btn-outline btn-sm graph-close" id="pn-graph-x">&#x2715;</button>
      <div class="graph-wrap" id="pn-graph-wrap"></div>
    </div>
  </div>
</div>`;

// ── Module entry ─────────────────────────────────────────────────────────
let keyHandler = null;   // one global Ctrl+S handler at a time, even if the module re-renders

export async function render(root, App) {
    if (!document.getElementById('pn-style')) {
        const st = document.createElement('style');
        st.id = 'pn-style'; st.textContent = CSS;
        document.head.appendChild(st);
    }
    root.innerHTML = TEMPLATE;
    const $ = s => root.querySelector(s);
    const S = App.Store;

    const el = {
        search: $('#pn-search'), tree: $('#pn-tree'), badge: $('#pn-badge'), syncBtn: $('#pn-sync-btn'), calBtn: $('#pn-cal-btn'),
        editor: $('#pn-editor'), preview: $('#pn-preview'), title: $('#pn-title'), titleView: $('#pn-title-view'), modified: $('#pn-modified'),
        status: $('#pn-status'), controls: $('#pn-controls'), save: $('#pn-save-btn'), del: $('#pn-del-btn'),
        segPreview: $('#pn-seg-preview'), segEdit: $('#pn-seg-edit'), toast: $('#pn-toast')
    };

    // Non-fatal: if offline, notes still open (as plain text) and edit normally.
    loadScript(CDN.marked).then(() => { if (isPreview && originalPath) showPreview(); }).catch(() => {});

    // ── Vault (all notes, in memory, mirrored to IndexedDB) ──────────────
    // notes[path] = { content, created, modified, h: hash(content), sh: hash at last sync, sha: GitHub blob sha }
    // deleted[path] = blob sha of a note deleted locally, waiting for the next sync to delete it on GitHub
    const vault = (await S.get('pn_vault')) || { notes: {}, deleted: {}, lastSynced: null };
    const persist = () => S.set('pn_vault', vault);
    const paths = () => Object.keys(vault.notes).sort();
    const isDirty = n => n.h !== n.sh;
    const pendingCount = () => Object.values(vault.notes).filter(isDirty).length + Object.keys(vault.deleted).length;

    function removeNote(path) {
        const n = vault.notes[path];
        if (n?.sha) vault.deleted[path] = n.sha;
        delete vault.notes[path];
    }

    // ── State ────────────────────────────────────────────────────────────
    let workingDir = null, originalPath = '', isNew = false, isPreview = true;
    let saveTimer = null, searchQuery = '', matching = null, searchTimer = null, syncing = false;
    let from = null, to = null;                         // applied date filter
    let calY, calM, calStart = null, calEnd = null, calCounts = {};
    const expanded = new Set();

    // ── Toast ────────────────────────────────────────────────────────────
    let toastTimer = null;
    function toast(msg, error) {
        el.toast.textContent = msg;
        el.toast.classList.toggle('error', !!error);
        el.toast.classList.add('show');
        clearTimeout(toastTimer);
        toastTimer = setTimeout(() => el.toast.classList.remove('show'), error ? 5000 : 2600);
    }

    // ── Sync badge ───────────────────────────────────────────────────────
    function relTime(iso) {
        const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
        if (s < 60) return 'just now';
        if (s < 3600) return Math.floor(s / 60) + 'm ago';
        if (s < 86400) return Math.floor(s / 3600) + 'h ago';
        return Math.floor(s / 86400) + 'd ago';
    }
    function updateBadge() {
        const n = pendingCount();
        el.badge.textContent = n > 99 ? '99+' : n;
        el.badge.style.display = n ? 'flex' : 'none';
        el.syncBtn.title = 'Sync with GitHub — ' + (vault.lastSynced ? 'last synced ' + relTime(vault.lastSynced) : 'never synced yet') + (n ? ` (${n} unsynced)` : '');
    }

    // ── File tree ────────────────────────────────────────────────────────
    const CHEVRON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 18 15 12 9 6"/></svg>';

    function buildTree() {
        const scroll = el.tree.scrollTop;
        el.tree.innerHTML = '';
        const visible = paths().filter(p => !matching || matching.has(p));
        const rootNode = { children: {}, items: [] };
        visible.forEach(path => {
            const parts = path.split('/');
            let node = rootNode;
            for (let i = 0; i < parts.length - 1; i++) node = (node.children[parts[i]] ??= { children: {}, items: [] });
            node.items.push({ path, label: parts.at(-1).replace(/\.md$/, '') });
        });

        // "Notes" root row: click = make the top level the working directory
        const rootHeader = document.createElement('div');
        rootHeader.className = 'folder-header' + (workingDir === '' ? ' selected-folder' : '');
        rootHeader.textContent = 'Notes';
        rootHeader.onclick = () => selectFolder('');
        const rootContent = document.createElement('div');
        rootContent.className = 'folder-content expanded';
        el.tree.append(rootHeader, rootContent);

        renderLevel(rootNode, rootContent, '', !!matching);
        if (!visible.length) {
            rootContent.insertAdjacentHTML('beforeend', `<div class="pn-empty">${matching ? 'No matching notes' : 'No notes yet. Create one, or press sync to pull from GitHub.'}</div>`);
        }
        $('#pn-new-btn').title = 'New note in ' + (workingDir ? workingDir + '/' : 'the top level (click a folder to change)');
        el.tree.scrollTop = scroll;
        updateBadge();
    }

    // Folder name = pick it as the working directory (opens it if closed; a second click on the
    // picked folder collapses it). Chevron = collapse/expand only. Opening a note never highlights its folder.
    function selectFolder(full) {
        if (full && workingDir === full) toggleFolder(full);
        else { workingDir = full; expandTo(full); }
        if (isNew) {   // a note still being created follows the picked folder
            el.title.value = (workingDir ? workingDir + '/' : '') + el.title.value.split('/').pop();
            refreshBanner();
        }
        buildTree();
    }
    function toggleFolder(full) { expanded.has(full) ? expanded.delete(full) : expanded.add(full); buildTree(); }
    function expandTo(folder) {
        const parts = folder ? folder.split('/') : [];
        parts.forEach((_, i) => expanded.add(parts.slice(0, i + 1).join('/')));
    }

    // Recursive: works for any folder depth (Work/Clients/Acme/…)
    function renderLevel(node, container, folder, forceOpen) {
        // Daily/ is pinned first and listed newest → oldest (years, months, days)
        const desc = folder === DAILY || folder.startsWith(DAILY + '/');
        Object.keys(node.children)
            .sort((a, b) => (!folder && a === DAILY ? -1 : !folder && b === DAILY ? 1 : desc ? b.localeCompare(a) : a.localeCompare(b)))
            .forEach(name => {
                const full = folder ? `${folder}/${name}` : name;
                const open = forceOpen || expanded.has(full);
                const header = document.createElement('div');
                header.className = 'folder-header' + (open ? ' expanded' : '') + (workingDir === full ? ' selected-folder' : '');
                header.title = full;
                const chevron = document.createElement('span');
                chevron.className = 'folder-chevron';
                chevron.innerHTML = CHEVRON;
                chevron.onclick = e => { e.stopPropagation(); toggleFolder(full); };
                const label = document.createElement('span');
                label.textContent = name;
                header.append(chevron, label);
                header.onclick = () => selectFolder(full);

                const content = document.createElement('div');
                content.className = 'folder-content' + (open ? ' expanded' : '');
                container.append(header, content);
                renderLevel(node.children[name], content, full, forceOpen);
            });

        (desc ? [...node.items].reverse() : node.items).forEach(({ path, label }) => {
            const div = document.createElement('div');
            const n = vault.notes[path];
            div.className = 'file-item' + (path === originalPath ? ' active' : '') + (n && isDirty(n) ? ' unsynced' : '') + (/\.conflict-\d{8}-\d{6}$/.test(label) ? ' conflict' : '');
            div.innerHTML = matching && searchQuery ? highlightText(label, searchQuery) : esc(label);
            div.title = path + (n && isDirty(n) ? ' (unsynced)' : '');
            div.dataset.path = path;
            div.onclick = e => { e.stopPropagation(); loadNote(path); };
            container.appendChild(div);
        });
    }

    const highlightText = (text, q) => esc(text).replace(new RegExp('(' + escRe(esc(q)) + ')', 'ig'), '<mark>$1</mark>');

    // ── Search (instant, in memory) + date filter ────────────────────────
    function runSearch(q) {
        searchQuery = q;
        if (!q && !from && !to) matching = null;
        else {
            const ql = q.toLowerCase();
            matching = new Set();
            for (const [p, n] of Object.entries(vault.notes)) {
                const d = (n.modified || '').slice(0, 10);
                if ((from || to) && (!d || (from && d < from) || (to && d > to))) continue;
                if (ql && !(p.toLowerCase().includes(ql) || n.content.toLowerCase().includes(ql))) continue;
                matching.add(p);
            }
        }
        buildTree();
        refreshHighlight();
    }
    el.search.oninput = () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => runSearch(el.search.value.trim()), 150); };

    function highlightMatches(rootEl, q) {
        if (!q) return null;
        const ql = q.toLowerCase(), walker = document.createTreeWalker(rootEl, NodeFilter.SHOW_TEXT), nodes = [];
        let nd, first = null;
        while ((nd = walker.nextNode())) if (nd.nodeValue.toLowerCase().includes(ql)) nodes.push(nd);
        nodes.forEach(node => {
            const text = node.nodeValue, lower = text.toLowerCase(), frag = document.createDocumentFragment();
            let last = 0, idx;
            while ((idx = lower.indexOf(ql, last)) !== -1) {
                if (idx > last) frag.appendChild(document.createTextNode(text.slice(last, idx)));
                const m = document.createElement('mark');
                m.className = 'search-hit'; m.textContent = text.slice(idx, idx + q.length);
                frag.appendChild(m); first ||= m; last = idx + q.length;
            }
            if (last < text.length) frag.appendChild(document.createTextNode(text.slice(last)));
            node.parentNode.replaceChild(frag, node);
        });
        return first;
    }
    function refreshHighlight() {
        if (!isPreview) return;
        el.preview.querySelectorAll('mark.search-hit').forEach(m => m.replaceWith(document.createTextNode(m.textContent)));
        el.preview.normalize();
        if (searchQuery) highlightMatches(el.preview, searchQuery);
    }

    // ── Preview / edit ───────────────────────────────────────────────────
    function showPreview(scrollToMatch) {
        el.preview.innerHTML = renderMd(el.editor.value);
        el.preview.querySelectorAll('a').forEach(a => {
            const href = a.getAttribute('href') || '';
            if (href.startsWith('note:')) {
                a.classList.add('note-link');
                if (!resolveNote(decodeURIComponent(href.slice(5)), paths())) a.classList.add('note-missing');
            } else if (/^https?:/i.test(href)) { a.target = '_blank'; a.rel = 'noopener noreferrer'; }
        });
        attachToc(); attachSections(); attachChecklists(); attachCopyButtons();
        if (/\\\(|\\\[|\$\$/.test(el.editor.value)) typesetMath();
        if (searchQuery) {
            const first = highlightMatches(el.preview, searchQuery);
            if (scrollToMatch && first) first.scrollIntoView({ behavior: 'smooth', block: 'center' });
        }
    }

    function setMode(mode, scrollToMatch) {
        isPreview = mode === 'preview';
        if (isPreview) {
            showPreview(scrollToMatch);
            el.editor.style.display = 'none'; el.preview.style.display = 'block';
        } else {
            el.preview.style.display = 'none'; el.editor.style.display = 'block';
            el.editor.focus();
        }
        el.segPreview.classList.toggle('on', isPreview);
        el.segEdit.classList.toggle('on', !isPreview);
    }

    function attachToc() {
        const p = [...el.preview.querySelectorAll('p')].find(x => x.textContent.trim() === '[TOC]');
        if (!p) return;
        const box = document.createElement('div'), ul = document.createElement('ul');
        box.className = 'toc';
        box.innerHTML = '<span class="toctitle">Table of Contents</span>';
        el.preview.querySelectorAll('h1').forEach(h => {
            const li = document.createElement('li'), a = document.createElement('a');
            a.href = '#'; a.textContent = h.textContent.replace(COLLAPSED, '');
            a.onclick = e => { e.preventDefault(); h.closest('.h1-section')?.classList.add('expanded'); h.scrollIntoView({ behavior: 'smooth', block: 'start' }); };
            li.appendChild(a); ul.appendChild(li);
        });
        box.appendChild(ul);
        p.replaceWith(box);
    }

    // Everything from one <h1> to the next becomes a collapsible section
    function attachSections() {
        const nodes = [...el.preview.childNodes], frag = document.createDocumentFragment();
        let body = null;
        nodes.forEach(node => {
            if (node.nodeType === 1 && node.tagName === 'H1') {
                const collapsed = node.textContent.includes(COLLAPSED);
                if (collapsed) node.innerHTML = node.innerHTML.replace(COLLAPSED, '');
                const section = document.createElement('div');
                section.className = 'h1-section' + (collapsed ? '' : ' expanded');
                const header = document.createElement('div');
                header.className = 'h1-header';
                header.innerHTML = '<svg class="h1-chevron" viewBox="0 0 18 18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><polyline points="4 7 9 12 14 7"/></svg>';
                header.appendChild(node);
                header.onclick = () => section.classList.toggle('expanded');
                body = document.createElement('div');
                body.className = 'h1-body';
                section.append(header, body);
                frag.appendChild(section);
            } else (body || frag).appendChild(node);
        });
        el.preview.innerHTML = '';
        el.preview.appendChild(frag);
    }

    // Clicking a rendered checkbox flips the Nth "- [ ]" line in the source
    function attachChecklists() {
        el.preview.querySelectorAll('input[type="checkbox"]').forEach((box, idx) => {
            box.disabled = false;
            box.addEventListener('change', () => {
                const lines = el.editor.value.split('\n'), re = /^(\s*[-*+]\s+\[)([ xX])(\]\s*.*)$/;
                let count = -1;
                for (let i = 0; i < lines.length; i++) {
                    if (re.test(lines[i]) && ++count === idx) { lines[i] = lines[i].replace(re, (m, a, _, b) => a + (box.checked ? 'x' : ' ') + b); break; }
                }
                el.editor.value = lines.join('\n');
                scheduleSave();
            });
        });
    }

    function attachCopyButtons() {
        el.preview.querySelectorAll('pre').forEach(pre => {
            const btn = document.createElement('button');
            btn.className = 'copy-code-btn'; btn.textContent = 'Copy';
            btn.onclick = async () => {
                await navigator.clipboard.writeText((pre.querySelector('code') ?? pre).innerText).catch(() => {});
                btn.textContent = 'Copied!'; setTimeout(() => btn.textContent = 'Copy', 1500);
            };
            pre.appendChild(btn);
        });
    }

    async function typesetMath() {
        try {
            if (!window.MathJax?.typesetPromise) {
                window.MathJax = { tex: { inlineMath: [['\\(', '\\)'], ['$$', '$$']], displayMath: [['\\[', '\\]']] }, chtml: { matchFontHeight: true } };
                await loadScript(CDN.mathjax);
            }
            await window.MathJax.startup?.promise;
            await window.MathJax.typesetPromise([el.preview]);
        } catch (e) { /* offline: formulas just stay as raw LaTeX */ }
    }

    // Links inside the preview: [[wiki]] links, in-page "#" anchors (must not hit the app router)
    el.preview.addEventListener('click', e => {
        const a = e.target.closest('a');
        if (!a) return;
        const href = a.getAttribute('href') || '';
        if (href.startsWith('note:')) { e.preventDefault(); openByName(decodeURIComponent(href.slice(5))); }
        else if (href.startsWith('#')) e.preventDefault();
    });

    async function openByName(name) {
        const match = resolveNote(name, paths());
        if (match) return loadNote(match);
        const clean = name.split('|')[0].trim().replace(/\.md$/i, '');
        if (!confirm(`Note "${clean}" does not exist. Create it?`)) return;
        await newNote();
        el.title.value = clean;   // [[links]] are top-level paths, not relative to the picked folder
        refreshBanner();
        el.editor.value = `# ${clean.split('/').pop()}\n\n`;
        setMode('edit');
    }

    // ── Banner: title (click to rename), mode toggle, delete ──
    function refreshBanner() {
        const has = isNew || !!originalPath;
        const name = el.title.value.split('/').pop().trim();
        el.titleView.textContent = name || (has ? 'Untitled note' : 'No note selected');
        el.titleView.classList.toggle('placeholder', !name);
        el.titleView.classList.toggle('static', !has);
        el.controls.style.display = has ? 'flex' : 'none';
        el.save.style.display = isNew ? '' : 'none';
        el.del.style.display = originalPath ? '' : 'none';
        if (el.title.style.display === 'none') el.modified.textContent = '';
        el.segPreview.classList.toggle('on', isPreview);
        el.segEdit.classList.toggle('on', !isPreview);
    }

    // The title shows only the note's name. Clicking it reveals the FULL path (folders included) to edit.
    let titleBefore = '', cancelTitle = false;
    function startTitleEdit() {
        if (!(isNew || originalPath) || el.title.style.display !== 'none') return;
        titleBefore = el.title.value;
        el.titleView.style.display = 'none';
        el.title.style.display = 'block';
        el.modified.textContent = 'Full path — use / to put the note in (or move it to) a folder · Enter to confirm, Esc to cancel';
        el.title.focus();
        el.title.setSelectionRange(el.title.value.length, el.title.value.length);
    }
    async function endTitleEdit(cancel) {
        if (el.title.style.display === 'none') return;
        el.title.style.display = 'none';
        el.titleView.style.display = '';
        if (cancel) el.title.value = titleBefore;
        else if (!isNew && originalPath && targetPath() !== originalPath) {
            if (targetPath()) await saveNote();   // rename / move now
            el.title.value = originalPath.replace(/\.md$/, '');   // also reverts if the name was empty or taken
        }
        refreshBanner();
    }
    el.titleView.onclick = startTitleEdit;
    el.title.onblur = () => { const c = cancelTitle; cancelTitle = false; endTitleEdit(c); };
    el.title.onkeydown = e => {
        if (e.key === 'Enter') { e.preventDefault(); el.title.blur(); }
        else if (e.key === 'Escape') { cancelTitle = true; el.title.blur(); }
    };

    // ── Open / create / save / delete ────────────────────────────────────
    async function flushSave() {
        if (!saveTimer) return;
        clearTimeout(saveTimer); saveTimer = null;
        await saveNote(true);
    }

    async function loadNote(path) {
        await flushSave();
        const n = vault.notes[path];
        if (!n) return;
        isNew = false; originalPath = path;
        expandTo(path.includes('/') ? path.slice(0, path.lastIndexOf('/')) : '');   // reveal it in the tree (no highlight)
        el.title.value = path.replace(/\.md$/, ''); el.editor.value = n.content; el.status.textContent = '';
        setMode('preview', true);
        refreshBanner(); buildTree();
    }

    async function newNote() {
        await flushSave();
        isNew = true; originalPath = '';
        el.title.value = workingDir ? workingDir + '/' : '';   // starts in the picked folder
        el.editor.value = ''; el.status.textContent = '';
        setMode('edit'); refreshBanner(); buildTree();
        startTitleEdit();
    }

    function scheduleSave() {   // existing notes autosave; new notes wait for Save
        if (isNew || !originalPath) return;
        clearTimeout(saveTimer);
        el.status.textContent = 'Saving…';
        saveTimer = setTimeout(() => { saveTimer = null; saveNote(true); }, 800);
    }
    el.editor.oninput = scheduleSave;

    function targetPath() {
        const raw = el.title.value.trim().replace(/\.md$/i, '');
        if (!raw || raw.endsWith('/')) return '';
        return raw.split('/').map(x => x.trim()).filter(Boolean).join('/') + '.md';
    }

    async function saveNote(contentOnly = false) {
        const path = contentOnly && originalPath ? originalPath : targetPath();   // autosave never renames mid-typing
        if (!path) { toast('Name the note first — click the title.', true); startTitleEdit(); return; }
        const renamed = originalPath && path !== originalPath;
        if ((isNew || renamed) && vault.notes[path]) { alert(`A note named "${path}" already exists.`); return; }

        const now = nowStr(), content = el.editor.value, h = hash(content);
        const prev = vault.notes[path] || (renamed && vault.notes[originalPath]) || null;
        if (renamed) removeNote(originalPath);

        const same = !renamed && prev && prev.h === h;
        if (!same) {
            // re-creating a note that is pending deletion on GitHub → treat as an update of that file
            const sha = (!renamed && prev?.sha) || vault.deleted[path];
            delete vault.deleted[path];
            vault.notes[path] = { content, created: prev?.created || now, modified: now, h, sh: renamed ? undefined : prev?.sh, sha };
            await persist();
        }
        isNew = false; originalPath = path;
        el.title.value = path.replace(/\.md$/, '');
        expandTo(path.includes('/') ? path.slice(0, path.lastIndexOf('/')) : '');   // nested folders open so the note is visible
        el.status.textContent = 'Saved';
        setTimeout(() => { if (el.status.textContent === 'Saved') el.status.textContent = ''; }, 2000);
        refreshBanner(); buildTree();
    }

    async function deleteNote() {
        if (!originalPath || !confirm(`Delete "${originalPath}"?`)) return;
        clearTimeout(saveTimer); saveTimer = null;
        removeNote(originalPath);
        await persist();
        originalPath = ''; isNew = false;
        if (workingDir && !paths().some(p => p.startsWith(workingDir + '/'))) workingDir = null;
        el.title.value = ''; el.editor.value = ''; el.status.textContent = '';
        setMode('preview');
        el.preview.innerHTML = '<div class="pn-hint">Note deleted. It will be removed from GitHub on the next sync.</div>';
        refreshBanner(); buildTree();
    }

    // Daily notes: the newest DAILY_KEEP stay directly in Daily/; older ones are filed into
    // Daily/Archive/<year>/<MM-Month>/ — this happens when a NEW daily note is created.
    const DAILY_KEEP = 10;
    const DAILY_RE = /^Daily\/(?:Archive\/)?(?:\d{4}\/\d{2}-[A-Za-z]+\/)?(\d{4})-(\d{2})-(\d{2})\.md$/;
    const LEGACY_DAILY_RE = /^Daily\/\d{4}\/\d{2}-[A-Za-z]+\//;   // earlier layout: Daily/<year>/<month>/…

    function tidyDaily() {
        const found = Object.keys(vault.notes).map(p => ({ p, m: p.match(DAILY_RE) })).filter(x => x.m && +x.m[2] >= 1 && +x.m[2] <= 12);
        found.sort((a, b) => b.m.slice(1).join('-').localeCompare(a.m.slice(1).join('-')));
        let moved = 0;
        found.forEach(({ p, m }, i) => {
            const [, y, mo, d] = m, file = `${y}-${mo}-${d}.md`;
            const np = i < DAILY_KEEP ? `${DAILY}/${file}` : `${DAILY}/Archive/${y}/${mo}-${MONTHS[+mo - 1]}/${file}`;
            if (np === p || vault.notes[np]) return;
            const n = vault.notes[p], sha = vault.deleted[np];
            removeNote(p);
            delete vault.deleted[np];   // moving back onto a path pending deletion → update it instead
            vault.notes[np] = { ...n, sh: undefined, sha };
            if (originalPath === p) { originalPath = np; el.title.value = np.replace(/\.md$/, ''); }
            moved++;
        });
        return moved;
    }

    async function openDaily() {
        await flushSave();
        const file = isoDate(new Date()) + '.md';
        let path = paths().find(p => DAILY_RE.test(p) && p.endsWith('/' + file));
        if (!path) {
            const d = new Date(), now = nowStr();
            const pretty = `${d.toLocaleDateString('en-US', { weekday: 'long' })}, ${pad(d.getDate())} ${MONTHS[d.getMonth()]} ${d.getFullYear()}`;
            const content = `# ${pretty}\n\n## Tasks\n- [ ] \n\n## Daily Notes\n\n`;
            path = `${DAILY}/${file}`;
            vault.notes[path] = { content, created: now, modified: now, h: hash(content), sh: undefined, sha: vault.deleted[path] };
            delete vault.deleted[path];
            const moved = tidyDaily();   // new daily note → archive whatever fell out of the last 10
            await persist();
            if (moved) toast(`Archived ${moved} older daily note${moved > 1 ? 's' : ''} into Daily/Archive/`);
        }
        expanded.add(DAILY);
        loadNote(path);
    }

    el.segPreview.onclick = () => { if (!isPreview) setMode('preview'); };
    el.segEdit.onclick = () => { if (isPreview) setMode('edit'); };
    el.save.onclick = () => saveNote();
    el.del.onclick = deleteNote;
    $('#pn-new-btn').onclick = newNote;
    $('#pn-daily-btn').onclick = openDaily;

    if (keyHandler) document.removeEventListener('keydown', keyHandler);
    keyHandler = e => {
        if (!root.isConnected) return document.removeEventListener('keydown', keyHandler);
        if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') { e.preventDefault(); clearTimeout(saveTimer); saveTimer = null; saveNote(); }
    };
    document.addEventListener('keydown', keyHandler);

    // ── Date-range calendar ──────────────────────────────────────────────
    const overlayCal = $('#pn-cal-overlay');

    function openCalendar() {
        const now = new Date();
        calY = now.getFullYear(); calM = now.getMonth(); calStart = from; calEnd = to;
        calCounts = {};
        Object.values(vault.notes).forEach(n => { const d = (n.modified || '').slice(0, 10); if (d) calCounts[d] = (calCounts[d] || 0) + 1; });
        renderCalendar();
        overlayCal.classList.add('open');
    }
    function renderCalendar() {
        $('#pn-cal-month').textContent = MONTHS[calM] + ' ' + calY;
        const lo = calStart && calEnd ? (calStart < calEnd ? calStart : calEnd) : calStart;
        const hi = calStart && calEnd ? (calStart < calEnd ? calEnd : calStart) : calStart;
        $('#pn-cal-label').textContent = lo && calEnd ? `${lo}  →  ${hi}` : lo ? `Start: ${lo} — pick an end date` : 'Select a date range';

        const grid = $('#pn-cal-grid');
        grid.innerHTML = ['Mo', 'Tu', 'We', 'Th', 'Fr', 'Sa', 'Su'].map(d => `<div class="cal-dow">${d}</div>`).join('');
        const first = (new Date(calY, calM, 1).getDay() + 6) % 7, days = new Date(calY, calM + 1, 0).getDate(), today = isoDate(new Date());
        for (let i = 0; i < first; i++) grid.insertAdjacentHTML('beforeend', '<div class="cal-day empty"></div>');
        for (let d = 1; d <= days; d++) {
            const ds = `${calY}-${pad(calM + 1)}-${pad(d)}`, cell = document.createElement('div');
            cell.className = 'cal-day' + (ds === today ? ' today' : '') + (lo && ds >= lo && ds <= hi ? ' in-range' : '') + (ds === lo || ds === hi ? ' edge' : '');
            cell.textContent = d;
            if (calCounts[ds]) cell.insertAdjacentHTML('beforeend', `<span class="cal-count">${calCounts[ds] > 9 ? '9+' : calCounts[ds]}</span>`);
            cell.onclick = () => { if (!calStart || calEnd) { calStart = ds; calEnd = null; } else calEnd = ds; renderCalendar(); };
            grid.appendChild(cell);
        }
    }
    function applyRange(clear) {
        if (clear || !calStart) from = to = null;
        else { const end = calEnd || calStart; from = calStart < end ? calStart : end; to = calStart < end ? end : calStart; }
        el.calBtn.classList.toggle('active', !!(from || to));
        overlayCal.classList.remove('open');
        runSearch(searchQuery);
    }
    el.calBtn.onclick = openCalendar;
    $('#pn-cal-x').onclick = () => overlayCal.classList.remove('open');
    $('#pn-cal-prev').onclick = () => { if (--calM < 0) { calM = 11; calY--; } renderCalendar(); };
    $('#pn-cal-next').onclick = () => { if (++calM > 11) { calM = 0; calY++; } renderCalendar(); };
    $('#pn-cal-clear').onclick = () => applyRange(true);
    $('#pn-cal-apply').onclick = () => applyRange(false);
    overlayCal.onclick = e => { if (e.target === overlayCal) overlayCal.classList.remove('open'); };

    // ── Graph view (d3-force, loaded only when opened) ───────────────────
    const overlayGraph = $('#pn-graph-overlay');
    let sim = null;

    async function openGraph() {
        const wrap = $('#pn-graph-wrap');
        overlayGraph.classList.add('open');
        wrap.innerHTML = '<div class="graph-empty">Loading…</div>';
        try { await loadScript(CDN.d3); } catch { wrap.innerHTML = '<div class="graph-empty">Could not load the graph library (offline?).</div>'; return; }

        const all = paths(), nodes = all.map(id => ({ id, label: id.split('/').pop().replace(/\.md$/, '') })), links = [], seen = new Set();
        all.forEach(p => {
            for (const m of vault.notes[p].content.matchAll(/\[\[(.+?)\]\]/g)) {
                const t = resolveNote(m[1], all);
                if (!t || t === p) continue;
                const key = [p, t].sort().join('\u0001');
                if (!seen.has(key)) { seen.add(key); links.push({ source: p, target: t }); }
            }
        });
        drawGraph(wrap, nodes, links);
    }

    function drawGraph(wrap, nodes, links) {
        wrap.innerHTML = '';
        if (!nodes.length) { wrap.innerHTML = '<div class="graph-empty">No notes yet — link them with [[double brackets]] to see the graph.</div>'; return; }
        const d3 = window.d3, W = wrap.clientWidth || 900, H = wrap.clientHeight || 600, size = 7;

        const byId = {};
        nodes.forEach(n => { n.degree = 0; byId[n.id] = n; });
        links.forEach(l => { byId[l.source].degree++; byId[l.target].degree++; });
        const maxDeg = Math.max(1, ...nodes.map(n => n.degree));
        const LIGHT = [149, 223, 219], DARK = [0, 90, 90];
        const shade = t => 'rgb(' + LIGHT.map((c, i) => Math.round(c + (DARK[i] - c) * t)).join(',') + ')';
        const tOf = d => Math.sqrt(d / maxDeg);

        const svg = d3.select(wrap).append('svg').attr('viewBox', [0, 0, W, H]);
        const zoomG = svg.append('g');
        svg.call(d3.zoom().scaleExtent([0.15, 6]).on('zoom', e => zoomG.attr('transform', e.transform)));

        if (sim) sim.stop();
        const pull = d => 0.008 + Math.min(0.1, d.degree * 0.02);
        sim = d3.forceSimulation(nodes)
            .force('link', d3.forceLink(links).id(d => d.id).distance(24).strength(0.75))
            .force('charge', d3.forceManyBody().strength(-16))
            .force('center', d3.forceCenter(W / 2, H / 2))
            .force('x', d3.forceX(W / 2).strength(pull))
            .force('y', d3.forceY(H / 2).strength(pull))
            .force('collide', d3.forceCollide().radius(size / 2 + 2.5).iterations(2));

        const linkSel = zoomG.append('g').selectAll('line').data(links).join('line').attr('class', 'g-link')
            .style('stroke', d => shade(tOf(Math.max(d.source.degree, d.target.degree))))
            .style('opacity', d => 0.18 + tOf(Math.max(d.source.degree, d.target.degree)) * 0.35);

        const nodeSel = zoomG.append('g').selectAll('g').data(nodes, d => d.id).join('g').call(
            d3.drag()
                .on('start', (e, d) => { if (!e.active) sim.alphaTarget(0.3).restart(); d.fx = d.x; d.fy = d.y; d._dragged = false; })
                .on('drag', (e, d) => { d.fx = e.x; d.fy = e.y; d._dragged = true; })
                .on('end', (e, d) => { if (!e.active) sim.alphaTarget(0); d.fx = d.fy = null; }));

        nodeSel.append('rect').attr('class', 'g-node').attr('width', size).attr('height', size)
            .attr('x', -size / 2).attr('y', -size / 2).attr('rx', 1.5).style('fill', d => shade(tOf(d.degree)));

        const wrapLabel = text => {   // 2 lines × ~11 chars, ellipsis if longer
            const words = text.split(/\s+/).filter(Boolean), lines = [];
            let cur = '';
            words.forEach(w => { const t = cur ? cur + ' ' + w : w; if (t.length > 11 && cur) { lines.push(cur); cur = w; } else cur = t; });
            if (cur) lines.push(cur);
            let out = lines.slice(0, 2).map(l => l.length > 11 ? l.slice(0, 10) + '…' : l);
            if (lines.length > 2) out[1] = out[1].replace(/…?$/, '…');
            return out.length ? out : [''];
        };
        const labelSel = nodeSel.append('text').attr('class', 'g-label').attr('text-anchor', 'middle').each(function (d) {
            const lines = wrapLabel(d.label), t = d3.select(this).attr('y', -size / 2 - 4 - (lines.length - 1) * 10);
            lines.forEach((l, i) => t.append('tspan').text(l).attr('x', 0).attr('dy', i ? 10 : 0));
        });

        const adj = new Set();
        links.forEach(l => { adj.add(l.source.id + '\u0001' + l.target.id); adj.add(l.target.id + '\u0001' + l.source.id); });
        const near = (a, b) => a.id === b.id || adj.has(a.id + '\u0001' + b.id);

        nodeSel.on('mouseenter', (e, d) => {
            nodeSel.select('.g-node').classed('dim', o => !near(d, o)).classed('nb', o => o.id !== d.id && near(d, o));
            labelSel.classed('dim', o => !near(d, o)).classed('nb', o => o.id !== d.id && near(d, o)).classed('show', o => o.id === d.id);
            linkSel.classed('dim', l => l.source.id !== d.id && l.target.id !== d.id).classed('hi', l => l.source.id === d.id || l.target.id === d.id);
        }).on('mouseleave', () => {
            nodeSel.select('.g-node').classed('dim', false).classed('nb', false);
            labelSel.classed('dim', false).classed('nb', false).classed('show', false);
            linkSel.classed('dim', false).classed('hi', false);
        }).on('click', (e, d) => {
            if (d._dragged) { d._dragged = false; return; }
            closeGraph(); loadNote(d.id);
        });

        sim.on('tick', () => {
            linkSel.attr('x1', d => d.source.x).attr('y1', d => d.source.y).attr('x2', d => d.target.x).attr('y2', d => d.target.y);
            nodeSel.attr('transform', d => `translate(${d.x},${d.y})`);
        });
    }
    function closeGraph() { overlayGraph.classList.remove('open'); if (sim) sim.stop(); }
    $('#pn-graph-btn').onclick = openGraph;
    $('#pn-graph-x').onclick = closeGraph;
    overlayGraph.onclick = e => { if (e.target === overlayGraph) closeGraph(); };

    // ── GitHub sync (manual) ─────────────────────────────────────────────
    async function getCfg() {
        const token = await S.get('gh_token'), repo = await S.get('gh_repo'), folder = (await S.get('gh_folder')) || '';
        if (!token || !repo || !repo.includes('/')) return null;
        const sub = [folder, PN_SUBDIR].map(s => (s || '').trim().replace(/^\/+|\/+$/g, '')).filter(Boolean).join('/');
        return { token, repo, prefix: sub ? sub + '/' : '' };
    }

    async function doSync({ token, repo, prefix }) {
        const api = async (url, opt = {}) => {
            const r = await fetch(`https://api.github.com/repos/${repo}/${url}`, {
                ...opt, cache: 'no-store',   // never serve a stale tree right after a push
                headers: { Authorization: `Bearer ${token}`, Accept: 'application/vnd.github+json', ...(opt.body ? { 'Content-Type': 'application/json' } : {}) }
            });
            if (r.status === 401) throw new Error('GitHub rejected the token (401). Check Settings.');
            if (!r.ok) { const e = new Error(`GitHub ${r.status}: ${(await r.text()).slice(0, 140)}`); e.status = r.status; throw e; }
            return r.json();
        };

        let tree;
        try { tree = await api('git/trees/HEAD?recursive=1'); }
        catch (e) { throw (e.status === 404 || e.status === 409) ? new Error(`Repo "${repo}" not found, or it has no commits yet. Check Settings.`) : e; }
        if (tree.truncated) throw new Error('Repository is too large for one listing call — this module expects a notes-sized repo.');

        const remote = {};   // rel path → blob sha
        tree.tree.forEach(t => {
            if (t.type === 'blob' && t.path.endsWith('.md') && (!prefix || t.path.startsWith(prefix))) remote[t.path.slice(prefix.length)] = t.sha;
        });

        const r = { pulled: [], pushed: [], removedLocal: [], removedRemote: [], conflicts: [], errors: [] };
        const now = nowStr(), conflicted = new Set();

        // 1. Pull remote adds/updates
        const toFetch = Object.entries(remote).filter(([rel, sha]) => vault.notes[rel]?.sha !== sha && vault.deleted[rel] !== sha);
        for (let i = 0; i < toFetch.length; i += 8) {
            await Promise.all(toFetch.slice(i, i + 8).map(async ([rel, sha]) => {
                try {
                    const text = b64ToStr((await api('git/blobs/' + sha)).content), rh = hash(text), n = vault.notes[rel];
                    if (n && isDirty(n)) {
                        if (n.h === rh) { n.sha = sha; n.sh = n.h; return; }   // same text on both sides: just adopt
                        const stamp = now.replace(/[-:]/g, '').replace('T', '-');
                        const cp = rel.replace(/\.md$/, '') + `.conflict-${stamp}.md`;
                        vault.notes[cp] = { content: text, created: now, modified: now, h: rh, sh: undefined, sha: undefined };
                        conflicted.add(rel);
                        r.conflicts.push(`${rel}: changed on both sides — GitHub's version saved as ${cp}`);
                        return;
                    }
                    delete vault.deleted[rel];   // remote changed after we deleted it → bring it back
                    vault.notes[rel] = { content: text, created: n?.created || now, modified: now, h: rh, sh: rh, sha };
                    r.pulled.push(rel);
                } catch (e) { r.errors.push(`${rel}: ${e.message}`); }
            }));
        }

        // 2. Remote deletions
        for (const [rel, n] of Object.entries(vault.notes)) {
            if (!n.sha || remote[rel]) continue;
            if (!isDirty(n)) { delete vault.notes[rel]; r.removedLocal.push(rel); }
            else { n.sha = undefined; r.conflicts.push(`${rel}: deleted on GitHub but edited here — it will be re-created on GitHub`); }
        }
        for (const rel of Object.keys(vault.deleted)) if (!remote[rel]) delete vault.deleted[rel];

        if (Object.keys(vault.notes).some(p => LEGACY_DAILY_RE.test(p))) tidyDaily();   // earlier Daily/<year>/<month> layout

        // 3. Push local adds/updates (sequential — parallel commits to one branch collide)
        for (const [rel, n] of Object.entries(vault.notes)) {
            if (!isDirty(n) || conflicted.has(rel)) continue;
            const { content, h } = n;
            try {
                const res = await api('contents/' + encPath(prefix + rel), {
                    method: 'PUT',
                    body: JSON.stringify({ message: `Update ${rel} via Private Notes`, content: strToB64(content), ...(n.sha ? { sha: n.sha } : {}) })
                });
                n.sha = res.content.sha; n.sh = h;
                r.pushed.push(rel);
            } catch (e) { r.errors.push(`${rel}: ${e.message}`); }
        }

        // 4. Push local deletions
        for (const [rel, sha] of Object.entries(vault.deleted)) {
            try {
                await api('contents/' + encPath(prefix + rel), { method: 'DELETE', body: JSON.stringify({ message: `Delete ${rel} via Private Notes`, sha }) });
                delete vault.deleted[rel];
                r.removedRemote.push(rel);
            } catch (e) { r.errors.push(`${rel}: ${e.message}`); }
        }

        vault.lastSynced = now;
        return r;
    }

    async function runSync() {
        if (syncing) return;
        const cfg = await getCfg();
        if (!cfg) return toast('Not configured — open Settings and add the GitHub repo (owner/name) and token.', true);
        syncing = true; el.syncBtn.classList.add('spin'); el.syncBtn.disabled = true;
        try {
            await flushSave();
            const r = await doSync(cfg);
            const parts = [];
            if (r.pulled.length) parts.push(r.pulled.length + ' pulled');
            if (r.pushed.length) parts.push(r.pushed.length + ' pushed');
            if (r.removedLocal.length) parts.push(r.removedLocal.length + ' removed here');
            if (r.removedRemote.length) parts.push(r.removedRemote.length + ' removed on GitHub');
            let msg = parts.join(', ') || 'Already up to date';
            if (r.conflicts.length) msg += ` — ${r.conflicts.length} conflict${r.conflicts.length > 1 ? 's' : ''} (see .conflict notes)`;
            if (r.errors.length) { msg += ` — ${r.errors.length} error${r.errors.length > 1 ? 's' : ''}: ${r.errors[0]}`; console.warn('Sync errors', r.errors); }
            if (r.conflicts.length) console.info('Sync conflicts', r.conflicts);
            toast(msg, !!(r.conflicts.length || r.errors.length));

            // the open note was updated from GitHub and has no unsaved edits → refresh it
            if (originalPath && r.pulled.includes(originalPath) && !isNew) {
                el.editor.value = vault.notes[originalPath].content;
                if (isPreview) showPreview();
            }
        } catch (e) {
            toast(e.message || 'Sync failed.', true);
        } finally {
            await persist();
            syncing = false; el.syncBtn.classList.remove('spin'); el.syncBtn.disabled = false;
            buildTree(); refreshBanner();
        }
    }
    el.syncBtn.onclick = runSync;

    // ── Go ───────────────────────────────────────────────────────────────
    expanded.add(DAILY);   // recent daily notes are visible straight away
    if (Object.keys(vault.notes).some(p => LEGACY_DAILY_RE.test(p))) {   // one-time: earlier Daily/<year>/<month> layout
        const n = tidyDaily();
        if (n) { await persist(); toast(`Reorganised ${n} daily note${n > 1 ? 's' : ''}: latest ${DAILY_KEEP} in Daily/, older in Daily/Archive/`); }
    }
    refreshBanner();
    buildTree();
    // First run on this browser: pull everything once so the vault isn't empty
    if (!vault.lastSynced && !Object.keys(vault.notes).length && await getCfg()) runSync();
}
