export const meta = { title: 'Live Transcript' };

/* ─────────────────────────────────────────────────────────────────────────
   Live Transcript — your whole day as text, tagged <mic> (you) and <sys> (everything the PC plays).

   - Starts by itself when the app opens (config.js → `background`), runs in the background
     while you use other modules, and streams every line to  <working directory>/Audio Notes/Transcript YYYY-MM-DD.txt
     (one file per day). Lines are written to disk every few seconds, so a crash loses almost nothing.
   - Engine = the browser's built-in speech recognition: no model files, no libraries, no workers, very low CPU.
     "Private" = on-device recognition (Chrome/Edge download a small language pack once).
   - <sys> = a browser share with audio (pick the call's tab, or Entire screen + "Also share system audio"): one pick per session.
     <mic> = the microphone, allowed once. Both streams are kept for the whole session (no repeated prompts).
   - Stop releases the microphone and system audio. Press Start (or reload) to begin again.
   - The text box is yours: select, edit, delete and copy freely. Edits change the on-screen copy only; the file on disk is append-only.
   - Working directory: chosen once in Settings. The folder name for transcripts is FOLDER below.
   - No CSS in this file: every class used here is defined in styles.css.
   ───────────────────────────────────────────────────────────────────────── */

const KEY_PREFS = 'transcript_prefs', FOLDER = 'Audio Notes', MAX_VIEW = 60000;
const LANGS = [['en-US', 'English (US)'], ['pl-PL', 'Polski'], ['de-DE', 'Deutsch']];   // one language for both sources

const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
// Builds that can recognize from an audio track (needed for <sys>) also have processLocally (on-device).
const HAS_TRACK = !!SR && 'processLocally' in SR.prototype;

// ── Helpers ───────────────────────────────────────────────────────────────
const p2 = n => String(n).padStart(2, '0');
const clock = ts => { const d = new Date(ts); return `${p2(d.getHours())}:${p2(d.getMinutes())}:${p2(d.getSeconds())}`; };
const dayOf = ts => { const d = new Date(ts); return `${d.getFullYear()}-${p2(d.getMonth() + 1)}-${p2(d.getDate())}`; };
const words = s => s.toLowerCase().match(/[\p{L}\p{N}']+/gu) || [];
const say = (o, msg, err) => { if (!o.quiet) toast(msg, err); };

// ── State (module level, independent of what is on screen) ─────────────────
const mk = name => ({ name, on: true, stream: null, track: null, rec: null, to: null, fails: 0, got: false, fatal: false, err: '', interim: '', busy: false });
const S = { mic: mk('mic'), sys: mk('sys') };
const prefs = { lang: 'en-US', local: true, save: true };
const ui = {};
let Store = null, Local = null, loaded = null, booted = false;
let running = false, busy = false, stoppedByUser = false, armed = false, watchIv = null, toastTo = null;
let view = '';                                   // what the text box shows (the tail of today); the full record is on disk
let pending = [], flushTo = null, flushing = false, disk = 'ok';   // lines waiting for the disk: {day, text}
let recentSys = [];                              // last <sys> lines, used to drop the mic's echo of the speakers

const usable = s => s.on && (HAS_TRACK || s.name === 'mic');
const live = s => HAS_TRACK ? s.track?.readyState === 'live' : s.name === 'mic';   // without track support the recognizer reads the mic itself
const state = s => !HAS_TRACK && s.name === 'sys' ? 'unsupported' : !s.on ? 'off' : s.fatal ? 'error' : live(s) ? (running ? 'live' : 'ready') : s.name === 'sys' ? 'not shared — click <sys>' : 'no mic';

function savePrefs() {
    Store?.set(KEY_PREFS, { lang: prefs.lang, local: prefs.local, save: prefs.save, mic: S.mic.on, sys: S.sys.on }).catch(() => {});
}
async function loadState() {
    const p = (await Store.get(KEY_PREFS)) || {};
    prefs.lang = LANGS.some(l => l[0] === p.lang) ? p.lang : 'en-US';
    prefs.local = p.local ?? true;
    prefs.save = p.save ?? true;
    S.mic.on = p.mic ?? true;
    S.sys.on = p.sys ?? true;
}
async function init(App) {
    Store = App.Store; Local = App.Storage.Local;
    await (loaded ??= loadState());
}

// ── UI (every call is guarded: the module may be off screen while it records) ──
function toast(msg, err) {
    const el = ui.toast;
    if (!el?.isConnected) return;
    el.textContent = msg; el.classList.toggle('error', !!err); el.classList.add('show');
    clearTimeout(toastTo); toastTo = setTimeout(() => el.classList.remove('show'), err ? 6500 : 3500);
}
const DISK = { ok: 'saving to file', wait: 'click anywhere to allow folder access', nofolder: 'no working directory — set it in Settings', error: 'disk error, retrying' };
function statusText() {
    if (!SR) return 'Speech recognition is not supported in this browser';
    if (busy) return 'Starting…';
    if (!running) return 'Stopped';
    const parts = [S.mic, S.sys].filter(s => s.on).map(s => `<${s.name}> ${state(s)}`);
    parts.push(prefs.save ? DISK[disk] : 'not saving to file');
    return parts.join(' · ');
}
// Colour of the status dot: idle (grey) · ok (teal) · warn (amber: something is not connected yet) · bad (red: needs attention)
function level() {
    if (!SR) return 'bad';
    if (!running) return 'idle';
    const srcs = [S.mic, S.sys].filter(usable);
    if (srcs.some(s => s.fatal) || (prefs.save && (disk === 'error' || disk === 'nofolder'))) return 'bad';
    if (busy || srcs.some(s => !live(s)) || (prefs.save && disk === 'wait')) return 'warn';
    return 'ok';
}
const DOT = { idle: '#9ca3af', ok: 'var(--teal-400)', warn: 'var(--amber)', bad: 'var(--red)' };
function paint() {
    if (!ui.root?.isConnected) return;
    ui.toggle.textContent = running ? 'Stop' : 'Start';
    ui.toggle.className = 'btn ' + (running ? 'btn-danger' : 'btn-primary');
    ui.toggle.disabled = !SR || busy;
    ui.dotc.style.background = DOT[level()];
    ui.dot.title = statusText();
    ui.dot.setAttribute('aria-label', statusText());
    ui.srcBtns.forEach(b => {
        const s = S[b.dataset.src], st = state(s), trouble = running && s.on && st !== 'live' && st !== 'unsupported';
        b.classList.toggle('on', s.on);
        b.style.background = trouble ? (st === 'error' ? 'var(--red)' : 'var(--amber)') : '';   // the failing source lights up itself
        b.style.color = trouble ? (st === 'error' ? '#fff' : 'var(--ink)') : '';
        const off = !HAS_TRACK && b.dataset.src === 'sys';
        b.disabled = off; b.style.opacity = off ? '.4' : '';
        if (off) b.title = 'This browser cannot recognize system audio — use a recent Chrome or Edge.';
    });
    ui.engBtns.forEach(b => b.classList.toggle('on', (b.dataset.eng === '1') === prefs.local));
    ui.saveBtns.forEach(b => b.classList.toggle('on', prefs.save));
    ui.lang.value = prefs.lang;
}
function paintLive() {
    if (!ui.root?.isConnected) return;
    ui.liveMic.textContent = S.mic.interim ? '<mic> ' + S.mic.interim : '';
    ui.liveSys.textContent = S.sys.interim ? '<sys> ' + S.sys.interim : '';
}

// ── Log: text box (tail only) + disk (everything) ─────────────────────────
function addView(line) {
    view += line;
    const ta = ui.ta, shown = ta?.isConnected, editing = shown && document.activeElement === ta;
    if (view.length > MAX_VIEW && !editing) {   // all-day sessions: keep the box small (the file has it all); never trim while you are in the box
        view = view.slice(-Math.floor(MAX_VIEW * 0.75));
        view = view.slice(view.indexOf('\n') + 1);
        if (shown) { ta.value = view; ta.scrollTop = ta.scrollHeight; }
        return;
    }
    if (shown) {
        const len = ta.value.length, stick = ta.scrollTop + ta.clientHeight >= ta.scrollHeight - 40;   // follow the end only if you are at the end
        const atEnd = ta.selectionStart === len && ta.selectionEnd === len;                              // caret at the very end keeps following
        ta.setRangeText(line, len, len, 'preserve');                                                   // new text goes to the end; your selection / caret stay put
        if (atEnd) ta.setSelectionRange(ta.value.length, ta.value.length);
        if (stick) ta.scrollTop = ta.scrollHeight;
        view = ta.value;   // keep the stored copy equal to what you see (it may contain your edits)
    }
}
function clearView() {   // clears the text box only; the file on disk is untouched
    view = '';
    if (ui.ta?.isConnected) { ui.ta.value = ''; ui.ta.focus(); }
}
function emit(t, tag, text) {
    const line = `[${clock(t)}] <${tag}> ${text}\n`;
    addView(line);
    if (prefs.save) {
        pending.push({ day: dayOf(t), text: line });
        if (!flushTo) flushTo = setTimeout(() => flush(), 3000);   // batch: at most one write every 3 s
    }
}
// The mic also hears the speakers when no headphones are used: drop mic lines that repeat a recent <sys> line.
function isEcho(text, t) {
    const w = words(text);
    if (w.length < 4) return false;
    return recentSys.some(r => Math.abs(t - r.t) < 10000 && w.filter(x => r.set.has(x)).length / w.length >= 0.8);
}
function onFinal(s, text) {
    const t = Date.now();
    if (s.name === 'sys') {
        recentSys.push({ t, set: new Set(words(text)) });
        if (recentSys.length > 10) recentSys.shift();
        emit(t, 'sys', text);
    } else if (S.sys.rec) setTimeout(() => { if (!isEcho(text, t)) emit(t, 'mic', text); }, 1200);   // let the <sys> line arrive first
    else emit(t, 'mic', text);
}

async function flush(interactive = false) {   // append everything pending to today's file; lines leave the queue only once written
    flushTo = null;
    if (flushing || !pending.length || !prefs.save) return;
    flushing = true;
    try {
        const dir = await Local.dir(FOLDER, interactive);
        while (pending.length) {
            const day = pending[0].day;
            let text = '', n = 0;
            while (n < pending.length && pending[n].day === day) text += pending[n++].text;
            const fh = await dir.getFileHandle(`Transcript ${day}.txt`, { create: true });
            const size = (await fh.getFile()).size;
            const w = await fh.createWritable({ keepExistingData: true });
            await w.write({ type: 'write', position: size, data: text });
            await w.close();                      // close() is what commits the data to disk
            pending.splice(0, n);
        }
        disk = 'ok';
    } catch (e) { diskFail(e); }
    flushing = false;
    paint();
}
function diskFail(e) {
    const m = e?.message;
    if (m === 'NO_FOLDER') { if (disk !== 'nofolder') toast('Choose the working directory in Settings — transcripts are saved there.', true); disk = 'nofolder'; }
    else if (m === 'NEEDS_PERMISSION') { disk = 'wait'; armGesture(); }
    else { disk = 'error'; console.warn('Transcript write failed', e); if (!flushTo) flushTo = setTimeout(() => flush(), 10000); }
}
async function checkDisk(gesture) {
    if (!prefs.save) return;
    try { await Local.dir(FOLDER, gesture); disk = 'ok'; flush(); } catch (e) { diskFail(e); }
    paint();
}
// A browser only asks for folder access (or a share) after a click. After a restart the next click anywhere does it.
function armGesture() {
    if (armed) return;
    armed = true;
    const evs = ['pointerdown', 'keydown'];
    const h = () => { armed = false; evs.forEach(ev => document.removeEventListener(ev, h, true)); resume(); };
    evs.forEach(ev => document.addEventListener(ev, h, true));
}
async function resume() {
    await checkDisk(true);
    if (!running && !stoppedByUser) await start(false, false); else watch();
}

// ── Audio sources ─────────────────────────────────────────────────────────
function attach(s, stream, track) {
    s.stream = stream; s.track = track;
    track.addEventListener('ended', () => {   // device unplugged, sleep/resume, or "Stop sharing" clicked: the watchdog re-acquires it
        if (s.track !== track) return;
        s.stream = s.track = null;
        paint();
    });
}
const releaseSrc = s => { s.stream?.getTracks().forEach(t => t.stop()); s.stream = s.track = null; };

async function grabMic(o) {
    try {
        const st = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 } });
        attach(S.mic, st, st.getAudioTracks()[0]);
        return true;
    } catch (e) { say(o, 'Microphone unavailable: ' + e.message, true); return false; }
}
// <sys>: the browser's share picker. Choose the call's tab (tick "Share tab audio") or Entire screen (tick "Also share system audio").
// The picker needs a click, so after an automatic start <sys> waits until you click <sys> or Start.
async function grabSys(o) {
    if (!o.gesture) return false;
    try {
        const st = await navigator.mediaDevices.getDisplayMedia({
            video: true, systemAudio: 'include',
            audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false }
        });
        st.getVideoTracks().forEach(t => { t.stop(); st.removeTrack(t); });   // audio only: no video frames are kept or processed
        const track = st.getAudioTracks()[0];
        if (!track) { st.getTracks().forEach(t => t.stop()); say(o, 'No audio was shared. In the picker choose a browser tab (or Entire screen) and tick "Share audio".', true); return false; }
        attach(S.sys, st, track);
        return true;
    } catch (e) {
        say(o, e.name === 'NotAllowedError' ? 'Sharing was cancelled — <sys> is idle. Click <sys> to try again.' : 'Could not capture system audio: ' + e.message, true);
        return false;
    }
}
// "Private" = on-device recognition. Fail closed: if it cannot be guaranteed, never fall back to the cloud silently.
async function ensureModel(o) {
    if (!prefs.local) return true;
    if (!HAS_TRACK) { say(o, 'This browser cannot guarantee on-device recognition. Update Chrome/Edge or switch Engine to Cloud.', true); return false; }
    const opt = { langs: [prefs.lang], processLocally: true };
    try {
        if (typeof SR.available === 'function') {
            const st = await SR.available(opt);
            if (st === 'unavailable') { say(o, `No on-device model for ${prefs.lang}. Pick another language or switch Engine to Cloud.`, true); return false; }
            if (st !== 'available') { say(o, `Downloading the on-device model for ${prefs.lang} — first time only…`); await SR.install(opt); }
        }
        return true;
    } catch (e) { say(o, `On-device model for ${prefs.lang} failed: ${e.message}`, true); return false; }
}
async function prepare(s, o) {
    s.fatal = false; s.fails = 0; s.err = '';
    if (HAS_TRACK && !live(s) && !(await (s.name === 'sys' ? grabSys(o) : grabMic(o)))) return false;
    if (!(await ensureModel(o))) { s.fatal = true; return false; }
    return true;
}

// ── Recognition ───────────────────────────────────────────────────────────
function retry(s, lived = 0) {   // recognizers end by themselves (silence, time limit): restart quietly, back off if they keep failing
    if (!running || !usable(s) || s.fatal || !live(s)) return paint();
    s.fails = lived > 5000 || s.got ? 0 : s.fails + 1;
    if (s.fails === 6) toast(`<${s.name}> keeps stopping${s.err ? ' (' + s.err + ')' : ''}.`, true);
    clearTimeout(s.to);
    s.to = setTimeout(() => beginRec(s), Math.min(5000, 100 * 2 ** s.fails));
}
function beginRec(s) {
    if (!running || !usable(s) || s.rec || !live(s)) return;
    clearTimeout(s.to);
    const rec = new SR();
    rec.continuous = true; rec.interimResults = true; rec.maxAlternatives = 1; rec.lang = prefs.lang;
    if (HAS_TRACK) rec.processLocally = prefs.local;
    const clone = HAS_TRACK ? s.track.clone() : null;   // the recognizer gets a clone, so the source itself is never stopped by it
    const t0 = Date.now();
    s.got = false;

    rec.onresult = e => {
        s.got = true;
        let interim = '';
        for (let i = e.resultIndex; i < e.results.length; i++) {
            const r = e.results[i], text = r[0].transcript.trim();
            if (r.isFinal) { if (text) onFinal(s, text); } else interim += ' ' + text;
        }
        s.interim = interim.trim();
        paintLive();
    };
    rec.onerror = e => {
        s.err = e.error;
        if (e.error === 'not-allowed' || e.error === 'service-not-allowed') { s.fatal = true; toast(`<${s.name}>: recognition was blocked (${e.error}). Check this site's microphone permission.`, true); }
        else if (e.error === 'language-not-supported') { s.fatal = true; toast(`<${s.name}>: ${prefs.lang} is not supported${prefs.local ? ' on-device' : ''}.`, true); }
    };
    rec.onend = () => {
        clone?.stop();
        if (s.rec === rec) s.rec = null;
        s.interim = '';
        paintLive();
        retry(s, Date.now() - t0);
    };

    try {
        HAS_TRACK ? rec.start(clone) : rec.start();
        s.rec = rec;
    } catch (e) {
        clone?.stop();
        s.err = e.message;
        retry(s);
    }
    paint();
}
const stopRecs = () => { for (const s of [S.mic, S.sys]) { clearTimeout(s.to); s.rec?.stop(); } };   // they restart themselves with the current settings

// Every 30 s: bring back a source that vanished (sleep/resume, headset change) and retry a failed disk write
function watch() {
    if (!running) return;
    for (const s of [S.mic, S.sys].filter(usable)) {
        if (s.rec || s.fatal || s.busy) continue;
        s.busy = true;
        (async () => {
            try { if (live(s) || await prepare(s, { gesture: false, quiet: true })) beginRec(s); }
            finally { s.busy = false; paint(); }
        })();
    }
    if (pending.length && !flushing && !flushTo && disk !== 'wait') flush();
}

// ── Actions ───────────────────────────────────────────────────────────────
async function start(gesture = true, rearm = true) {
    if (running || busy || !SR) return;
    if (gesture) stoppedByUser = false;
    const list = [S.sys, S.mic].filter(usable);   // <sys> first: the share picker must open right after the click
    if (!list.length) { toast('Turn on <mic> or <sys> first.', true); return; }
    busy = true; paint();
    const ok = [];
    for (const s of list) if (await prepare(s, { gesture })) ok.push(s);
    busy = false;
    if (!ok.length) { paint(); if (rearm && !gesture) armGesture(); return; }
    running = true;
    ok.forEach(beginRec);
    clearInterval(watchIv); watchIv = setInterval(watch, 30000);
    checkDisk(gesture);
    paint();
}
function stop() {   // stops recognition AND releases the microphone / system audio
    stoppedByUser = true; running = false;
    clearInterval(watchIv);
    for (const s of [S.mic, S.sys]) { clearTimeout(s.to); s.rec?.stop(); s.interim = ''; releaseSrc(s); }
    flush();
    paint(); paintLive();
}
async function toggleSrc(s) {
    if (s.on && running && usable(s) && !live(s)) {   // switched on but not connected: a click opens the share picker
        if (await prepare(s, { gesture: true })) beginRec(s);
        paint(); return;
    }
    s.on = !s.on; savePrefs();
    if (!s.on) { clearTimeout(s.to); s.rec?.stop(); releaseSrc(s); }
    else if (running && usable(s) && await prepare(s, { gesture: true })) beginRec(s);
    if (running && !S.mic.on && !S.sys.on) stop();
    paint();
}
async function setEngine(local) {
    if (prefs.local === local) return;
    prefs.local = local; savePrefs(); paint();
    if (running && !(await ensureModel({}))) { prefs.local = !local; savePrefs(); paint(); return; }
    stopRecs();
}
async function setLang(lang) {
    const old = prefs.lang;
    if (lang === old) return;
    prefs.lang = lang; savePrefs();
    if (running && !(await ensureModel({}))) { prefs.lang = old; savePrefs(); paint(); return; }
    paint(); stopRecs();
}
async function setSave(on) {
    if (on === prefs.save) return;
    if (!on) await flush();   // write what is already captured, then stop writing
    prefs.save = on; savePrefs();
    if (on) checkDisk(true); else pending = [];
    paint();
}

// ── Screen ────────────────────────────────────────────────────────────────
const TEMPLATE = `
<div class="editor-panel fill">
  <div class="banner">
    <div class="banner-main" style="display:flex;flex-wrap:wrap;align-items:center;gap:6px">
      <button class="btn btn-primary btn-sm" id="lt-toggle" style="height:30px;min-width:64px">Start</button>
      <div class="seg"><button data-src="mic" title="Your microphone">&lt;mic&gt;</button><button data-src="sys" title="Everything the PC plays (share a tab or the screen with audio)">&lt;sys&gt;</button></div>
      <select class="input" id="lt-lang" title="Language" style="width:auto;height:30px;font-size:.75rem;padding:0 6px">${LANGS.map(([v, n]) => `<option value="${v}">${n}</option>`).join('')}</select>
      <div class="seg"><button data-eng="1" title="On-device: your audio never leaves this computer">Private</button><button data-eng="0" title="The browser's online speech service: usually more accurate, but audio is sent to Google / Microsoft">Cloud</button></div>
      <div class="seg"><button data-save title="Write the transcript to a .txt file in the Audio Notes folder">Save to file</button></div>
      <button class="btn btn-outline btn-sm" id="lt-clear" style="height:30px" title="Clear the text box (the saved file is not touched)">Clear</button>
    </div>
    <div class="banner-actions"><button class="btn-icon" id="lt-dot" aria-label="Status"><span id="lt-dotc" style="display:block;width:10px;height:10px;border-radius:50%;background:#9ca3af"></span></button></div>
  </div>
  <textarea class="editor-textarea" id="lt-log" spellcheck="false" placeholder="The transcript appears here. It starts by itself and is saved to the “Audio Notes” folder inside your working directory. You can select, edit and copy the text."></textarea>
  <div class="hint" style="min-height:2.9em"><div id="lt-live-mic"></div><div id="lt-live-sys"></div></div>
</div>
<div class="toast" id="lt-toast"></div>`;

export async function render(root, App) {
    await init(App);
    root.innerHTML = TEMPLATE;
    const $ = s => root.querySelector(s), all = s => [...root.querySelectorAll(s)];
    Object.assign(ui, {
        root, ta: $('#lt-log'), toggle: $('#lt-toggle'), dot: $('#lt-dot'), dotc: $('#lt-dotc'), toast: $('#lt-toast'), lang: $('#lt-lang'),
        liveMic: $('#lt-live-mic'), liveSys: $('#lt-live-sys'),
        srcBtns: all('[data-src]'), engBtns: all('[data-eng]'), saveBtns: all('[data-save]')
    });
    ui.ta.value = view;
    ui.ta.scrollTop = ui.ta.scrollHeight;
    ui.srcBtns.forEach(b => b.onclick = () => toggleSrc(S[b.dataset.src]));
    ui.engBtns.forEach(b => b.onclick = () => setEngine(b.dataset.eng === '1'));
    ui.saveBtns.forEach(b => b.onclick = () => setSave(!prefs.save));
    ui.ta.oninput = () => { view = ui.ta.value; };   // your edits are kept when you leave and come back
    $('#lt-clear').onclick = clearView;
    ui.dot.onclick = () => toast(statusText(), level() === 'bad');   // tap the dot for the details (works on mobile too)
    ui.lang.onchange = () => setLang(ui.lang.value);
    ui.toggle.onclick = () => running ? stop() : start(true);
    paint(); paintLive();
}

// Called once by app.js when the app opens (config.js → background): this is what makes it record all day.
export async function boot(App) {
    await init(App);
    if (booted || !SR) return;
    booted = true;
    navigator.mediaDevices?.addEventListener?.('devicechange', () => setTimeout(watch, 1500));
    window.addEventListener('app:folder-changed', () => checkDisk(true));   // fired by Settings after you pick the working directory
    start(false);
}
