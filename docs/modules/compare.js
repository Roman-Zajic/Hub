export const meta = { title: 'Compare Texts' };

/* ─────────────────────────────────────────────────────────────────────────
   Compare Texts — a two-text diff (like diffchecker.com) for code and e-mails.

   - Step 1 compares LINES (Myers' algorithm), step 2 compares the WORDS (or characters)
     inside every pair of changed lines, so single changed fragments are highlighted too.
   - Views: Split / Unified. Highlight: Line / Word / Character.
   - Toggles: hide unchanged lines (click a fold to open it), wrap long lines.
   - Open a file (button or drag it onto a text box), jump between changes.
   - Texts and options stay in memory while the app is open (they survive visiting other modules).
   - No CSS in this file: the few new classes (df-*) are in the "Compare Texts" block appended to styles.css.
   ───────────────────────────────────────────────────────────────────────── */

const MAX_EDITS = 3000;   // more edits than this = "completely different" (keeps time and memory bounded)
const CONTEXT = 3;        // unchanged lines kept around a change when unchanged lines are hidden
const TOKEN = /\s+|[\p{L}\p{N}_]+|[^\s\p{L}\p{N}_]/gu;   // a word, a run of spaces, or one punctuation mark (Polish/German letters count as letters)

const S = {   // state: module level, so it survives switching modules
    a: '', b: '', res: null,
    view: window.matchMedia?.('(max-width: 768px)').matches ? 'unified' : 'split',
    unit: 'word', hide: false, wrap: true
};

let esc = s => s;   // replaced by the shared App.esc when the module is rendered

// ── Diff engine ──────────────────────────────────────────────────────────
// Myers' O(ND) algorithm. a, b: arrays of comparable keys → [['=' | '-' | '+', indexInA, indexInB], …]
function myers(a, b) {
    const n = a.length, m = b.length;
    const all = () => [...a.map((_, i) => ['-', i, 0]), ...b.map((_, j) => ['+', n, j])];
    if (!n || !m) return all();
    const max = Math.min(n + m, MAX_EDITS), off = max + 1;
    const v = new Int32Array(2 * max + 3), trace = [];   // v[off + k] = furthest x reached on diagonal k
    let found = false;
    for (let d = 0; d <= max && !found; d++) {
        trace.push(v.slice());
        for (let k = -d; k <= d; k += 2) {
            let x = (k === -d || (k !== d && v[off + k - 1] < v[off + k + 1])) ? v[off + k + 1] : v[off + k - 1] + 1;
            let y = x - k;
            while (x < n && y < m && a[x] === b[y]) { x++; y++; }
            v[off + k] = x;
            if (x >= n && y >= m) { found = true; break; }
        }
    }
    if (!found) return all();
    const ops = [];
    let x = n, y = m;
    for (let d = trace.length - 1; d >= 0; d--) {   // walk back from the end to the start
        const vd = trace[d], k = x - y;
        const prevK = (k === -d || (k !== d && vd[off + k - 1] < vd[off + k + 1])) ? k + 1 : k - 1;
        const prevX = vd[off + prevK], prevY = prevX - prevK;
        while (x > prevX && y > prevY) { x--; y--; ops.push(['=', x, y]); }
        if (d > 0) ops.push(x === prevX ? ['+', prevX, prevY] : ['-', prevX, prevY]);
        x = prevX; y = prevY;
    }
    return ops.reverse();
}

// Same, but the identical beginning and end are skipped first (most texts differ only in the middle)
function diffKeys(a, b) {
    let start = 0, endA = a.length, endB = b.length;
    while (start < endA && start < endB && a[start] === b[start]) start++;
    while (endA > start && endB > start && a[endA - 1] === b[endB - 1]) { endA--; endB--; }
    const ops = [];
    for (let i = 0; i < start; i++) ops.push(['=', i, i]);
    for (const [t, i, j] of myers(a.slice(start, endA), b.slice(start, endB))) ops.push([t, i + start, j + start]);
    for (let i = endA, j = endB; i < a.length; i++, j++) ops.push(['=', i, j]);
    return ops;
}

const lines = t => t ? t.replace(/\r\n?/g, '\n').replace(/\n$/, '').split('\n') : [];
const tokens = (s, unit) => unit === 'char' ? [...s] : s.match(TOKEN) || [];
const plural = (c, w) => `${c} ${w}${c === 1 ? '' : 's'}`;

const pushSeg = (segs, text, mark) => {   // neighbouring pieces with the same mark become one
    const last = segs[segs.length - 1];
    if (last && last.mark === mark) last.text += text; else segs.push({ text, mark });
};

// Two marked pieces with only spaces between them read better as one marked phrase ("on" + " " + "Monday")
const joinGaps = segs => segs.reduce((out, s) => {
    const gap = out[out.length - 1], before = out[out.length - 2];
    if (s.mark && gap && !gap.mark && !gap.text.trim() && before?.mark) { before.text += gap.text + s.text; out.pop(); }
    else out.push(s);
    return out;
}, []);

// Marks the changed fragments inside a removed line (l) and the added line that replaces it (r)
function markFragments(l, r, unit) {
    const ta = tokens(l.text, unit), tb = tokens(r.text, unit);
    const left = [], right = [];
    let same = 0;
    for (const [t, i, j] of diffKeys(ta, tb)) {
        if (t === '=') { pushSeg(left, ta[i], false); pushSeg(right, tb[j], false); same += ta[i].replace(/\s/g, '').length; }
        else if (t === '-') pushSeg(left, ta[i], true);
        else pushSeg(right, tb[j], true);
    }
    // Two lines with hardly anything in common are just "replaced": a confetti of fragments would not help
    const size = Math.max(l.text.replace(/\s/g, '').length, r.text.replace(/\s/g, '').length);
    if (size && same / size >= 0.3) { l.segs = joinGaps(left); r.segs = joinGaps(right); }
}

// Result: rows for the split view. { type: 'eq' | 'chg', g: change number, l: line | null, r: line | null }, a line = { n, text, segs? }
function compare(textA, textB, unit) {
    const A = lines(textA), B = lines(textB);
    const res = { rows: [], groups: 0, dels: 0, adds: 0 };
    let dels = [], adds = [];
    const flush = () => {   // one block of changes: the first removed line sits next to the first added line, and so on
        const count = Math.max(dels.length, adds.length);
        for (let k = 0; k < count; k++) {
            const l = dels[k] || null, r = adds[k] || null;
            if (l && r && unit !== 'line') markFragments(l, r, unit);
            res.rows.push({ type: 'chg', g: res.groups, l, r });
        }
        if (count) res.groups++;
        res.dels += dels.length; res.adds += adds.length;
        dels = []; adds = [];
    };
    for (const [t, i, j] of diffKeys(A, B)) {
        if (t === '-') dels.push({ n: i + 1, text: A[i] });
        else if (t === '+') adds.push({ n: j + 1, text: B[j] });
        else { flush(); res.rows.push({ type: 'eq', l: { n: i + 1, text: A[i] }, r: { n: j + 1, text: B[j] } }); }
    }
    flush();
    return res;
}

// ── Drawing the result ───────────────────────────────────────────────────
const frag = line => line.segs ? line.segs.map(s => s.mark ? `<mark>${esc(s.text)}</mark>` : esc(s.text)).join('') : esc(line.text);

const cell = (line, cls) => line
    ? `<td class="df-n">${line.n}</td><td class="df-t ${cls}">${frag(line)}</td>`
    : '<td class="df-n"></td><td class="df-t df-none"></td>';
const splitEq = r => `<tr>${cell(r.l, '')}${cell(r.r, '')}</tr>`;
const splitChg = r => `<tr data-chg="${r.g}">${cell(r.l, 'df-del')}${cell(r.r, 'df-add')}</tr>`;

const KIND = { '-': 'df-del', '+': 'df-add' };
const unifiedEq = r => `<tr><td class="df-n">${r.l.n}</td><td class="df-n">${r.r.n}</td><td class="df-s"></td><td class="df-t">${frag(r.r)}</td></tr>`;
const unifiedLine = (sign, line, g) => `<tr data-chg="${g}"><td class="df-n">${sign === '-' ? line.n : ''}</td><td class="df-n">${sign === '+' ? line.n : ''}</td><td class="df-s ${KIND[sign]}">${sign}</td><td class="df-t ${KIND[sign]}">${frag(line)}</td></tr>`;
const unifiedChg = block => [   // all removed lines first, then all added lines
    ...block.filter(r => r.l).map(r => unifiedLine('-', r.l, r.g)),
    ...block.filter(r => r.r).map(r => unifiedLine('+', r.r, r.g))
].join('');

// Which rows are shown: all of them, or (unchanged lines hidden) the changes ± CONTEXT lines plus the folds you opened
function visibility(rows, hide, unfolded) {
    if (!hide) return rows.map(() => true);
    const show = rows.map(r => r.type === 'chg');
    rows.forEach((r, i) => {
        if (r.type !== 'chg') return;
        for (let k = Math.max(0, i - CONTEXT); k <= Math.min(rows.length - 1, i + CONTEXT); k++) show[k] = true;
    });
    for (let i = 0; i < show.length; i++) {
        if (show[i]) continue;
        let j = i;
        while (j < show.length && !show[j]) j++;
        if (unfolded.has(i)) for (let k = i; k < j; k++) show[k] = true;
        i = j;
    }
    return show;
}

function renderTable(rows, o, unfolded) {
    const split = o.view === 'split';
    const show = visibility(rows, o.hide, unfolded);
    let html = '', i = 0;
    while (i < rows.length) {
        let j = i;
        if (!show[i]) {   // a run of hidden unchanged lines
            while (j < rows.length && !show[j]) j++;
            html += `<tr class="df-fold" data-from="${i}"><td colspan="4">${plural(j - i, 'unchanged line')} — click to show</td></tr>`;
        } else if (rows[i].type === 'eq') {
            j++;
            html += split ? splitEq(rows[i]) : unifiedEq(rows[i]);
        } else {   // a block of changes
            while (j < rows.length && rows[j].type === 'chg') j++;
            const block = rows.slice(i, j);
            html += split ? block.map(splitChg).join('') : unifiedChg(block);
        }
        i = j;
    }
    const cols = split ? '<col class="df-cn"><col><col class="df-cn"><col>' : '<col class="df-cn"><col class="df-cn"><col class="df-cs"><col>';
    return `<table class="df-table"><colgroup>${cols}</colgroup><tbody>${html}</tbody></table>`;
}

// ── Screen ───────────────────────────────────────────────────────────────
const icon = paths => `<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${paths}</svg>`;
const ICON = {
    wrap: icon('<line x1="3" y1="6" x2="21" y2="6"/><path d="M3 12h15a3 3 0 0 1 0 6h-4"/><polyline points="16 16 14 18 16 20"/><line x1="3" y1="18" x2="10" y2="18"/>'),
    hide: icon('<polyline points="7 20 12 15 17 20"/><polyline points="7 4 12 9 17 4"/>'),
    up: icon('<polyline points="18 15 12 9 6 15"/>'),
    down: icon('<polyline points="6 9 12 15 18 9"/>')
};

const TEMPLATE = `
<div class="editor-panel fill df">
    <div class="banner">
        <div class="banner-main">
            <span class="banner-title static">Compare texts</span>
            <span class="banner-status" id="df-status"></span>
        </div>
        <div class="banner-actions">
            <div class="toolbar-group">
                <div class="seg" role="group" aria-label="View"><button type="button" data-view="split">Split</button><button type="button" data-view="unified">Unified</button></div>
                <div class="seg" role="group" aria-label="Highlight"><button type="button" data-unit="line">Line</button><button type="button" data-unit="word">Word</button><button type="button" data-unit="char">Character</button></div>
            </div>
            <div class="toolbar-group">
                <button type="button" class="btn-icon" data-toggle="hide" title="Hide unchanged lines">${ICON.hide}</button>
                <button type="button" class="btn-icon" data-toggle="wrap" title="Wrap long lines">${ICON.wrap}</button>
            </div>
            <div class="toolbar-group" data-result>
                <button type="button" class="btn-icon" id="df-prev" title="Previous change">${ICON.up}</button>
                <button type="button" class="btn-icon" id="df-next" title="Next change">${ICON.down}</button>
                <span class="banner-status" id="df-count"></span>
            </div>
            <div class="toolbar-group" data-input>
                <button class="btn btn-outline btn-sm" data-open="a">Open original</button>
                <button class="btn btn-outline btn-sm" data-open="b">Open changed</button>
            </div>
            <button class="btn btn-outline btn-sm" id="df-clear">Clear</button>
            <button class="btn btn-outline btn-sm" id="df-edit" data-result>Edit input</button>
            <button class="btn btn-primary btn-sm" id="df-run" data-input title="Ctrl+Enter">Find difference</button>
        </div>
    </div>

    <div class="split" data-input>
        <textarea id="df-a" class="editor-textarea" spellcheck="false" placeholder="Original text: paste it here, or drop a file."></textarea>
        <textarea id="df-b" class="editor-textarea" spellcheck="false" placeholder="Changed text: paste it here, or drop a file."></textarea>
    </div>
    <div class="df-result" id="df-result" data-result></div>
    <input type="file" id="df-file" hidden>
</div>`;

export async function render(root, App) {
    esc = App.esc;
    root.innerHTML = TEMPLATE;
    const $ = s => root.querySelector(s);
    const boxes = { a: $('#df-a'), b: $('#df-b') };
    const result = $('#df-result'), status = $('#df-status'), count = $('#df-count'), fileInput = $('#df-file');
    let unfolded = new Set(), current = -1, openFor = 'a';

    boxes.a.value = S.a; boxes.b.value = S.b;
    const sync = () => { S.a = boxes.a.value; S.b = boxes.b.value; };

    function draw() {
        const res = S.res;
        root.querySelectorAll('[data-result]').forEach(el => { el.hidden = !res; });
        root.querySelectorAll('[data-input]').forEach(el => { el.hidden = !!res; });
        root.querySelectorAll('[data-view]').forEach(b => b.classList.toggle('on', b.dataset.view === S.view));
        root.querySelectorAll('[data-unit]').forEach(b => b.classList.toggle('on', b.dataset.unit === S.unit));
        root.querySelectorAll('[data-toggle]').forEach(b => { const on = S[b.dataset.toggle]; b.classList.toggle('active', on); b.setAttribute('aria-pressed', on); });
        result.classList.toggle('wrap', S.wrap);
        status.textContent = res ? `${plural(res.dels, 'removal')} · ${plural(res.adds, 'addition')}` : '';
        count.textContent = ''; current = -1;
        if (!res) return;
        const top = result.scrollTop;   // keep the position when a fold is opened or an option changes
        result.innerHTML = res.groups ? renderTable(res.rows, S, unfolded) : '<div class="hint">No differences found.</div>';
        result.scrollTop = top;
    }

    function run() {
        sync();
        if (!S.a && !S.b) return App.toast('Paste or open two texts first.', true);
        S.res = compare(S.a, S.b, S.unit);
        unfolded = new Set();
        draw();
    }

    function go(step) {   // jump to the next / previous change
        const total = S.res?.groups;
        if (!total) return;
        result.querySelectorAll('.df-current').forEach(r => r.classList.remove('df-current'));
        current = current < 0 ? (step > 0 ? 0 : total - 1) : (current + step + total) % total;
        const rows = result.querySelectorAll(`[data-chg="${current}"]`);
        rows.forEach(r => r.classList.add('df-current'));
        rows[0].scrollIntoView({ block: 'center' });
        count.textContent = `${current + 1} / ${total}`;
    }

    // ── buttons and options ──
    $('#df-run').onclick = run;
    $('#df-edit').onclick = () => { S.res = null; draw(); boxes.a.focus(); };
    $('#df-clear').onclick = () => { boxes.a.value = boxes.b.value = ''; sync(); S.res = null; draw(); };
    $('#df-prev').onclick = () => go(-1);
    $('#df-next').onclick = () => go(1);

    root.querySelectorAll('[data-view]').forEach(b => { b.onclick = () => { S.view = b.dataset.view; draw(); }; });
    root.querySelectorAll('[data-unit]').forEach(b => { b.onclick = () => { S.unit = b.dataset.unit; if (S.res) run(); else draw(); }; });

    root.querySelectorAll('[data-toggle]').forEach(b => {   // on / off buttons: hide unchanged lines, wrap long lines
        b.onclick = () => {
            const key = b.dataset.toggle;
            S[key] = !S[key];
            if (key === 'hide') unfolded = new Set();
            draw();
        };
    });

    result.onclick = e => {   // open a fold of hidden unchanged lines
        const fold = e.target.closest('.df-fold');
        if (!fold) return;
        unfolded.add(+fold.dataset.from);
        draw();
    };

    // ── texts: typing, files, drag & drop ──
    async function load(box, file) {
        if (!file) return;
        try { box.value = await file.text(); sync(); } catch { App.toast('Could not read ' + file.name, true); }
    }
    root.querySelectorAll('[data-open]').forEach(b => { b.onclick = () => { openFor = b.dataset.open; fileInput.value = ''; fileInput.click(); }; });
    fileInput.onchange = () => load(boxes[openFor], fileInput.files[0]);
    for (const box of Object.values(boxes)) {
        box.oninput = sync;
        box.onkeydown = e => { if (e.ctrlKey && e.key === 'Enter') run(); };
        box.ondragover = e => e.preventDefault();
        box.ondrop = e => { const file = e.dataTransfer.files[0]; if (file) { e.preventDefault(); load(box, file); } };
    }

    // Coming back to the module: show the last comparison again
    if (S.res) S.res = compare(S.a, S.b, S.unit);
    draw();
}
