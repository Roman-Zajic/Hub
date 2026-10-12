export const meta = { title: 'Timers' };

/* ─────────────────────────────────────────────────────────────────────────
   Timers — a dashboard of countdowns, stopwatches, alarms and pomodoro timers.

   - Timers store ABSOLUTE timestamps (when they end / when they started), never "ticks",
     so reloads, background tabs and sleeping laptops can't make them drift.
   - State lives at module level: timers keep running (and ringing) while you visit other
     modules. They start working once this module has been opened in the session.
   - Saved per device (App.Store key 'timers_state').
   - Each timer can pop out into its own window (same stylesheet, own display loop, own
     full-screen button) — handy on a second monitor.
   - Sound is generated with the Web Audio API: no audio files.
   - No CSS in this file: every class used here is defined in styles.css.
   ───────────────────────────────────────────────────────────────────────── */

const STORE_KEY = 'timers_state';
const TYPES = { countdown: 'Countdown', stopwatch: 'Stopwatch', alarm: 'Alarm', pomodoro: 'Pomodoro' };
const PHASES = { focus: 'Focus', short: 'Short break', long: 'Long break' };
const PRESETS = {
    countdown: { title: 'Countdown', duration: 10 * 60000, sound: true },
    stopwatch: { title: 'Stopwatch' },
    alarm: { title: 'Alarm', time: '07:00', repeat: false, sound: true },
    pomodoro: { title: 'Pomodoro', focus: 25, short: 5, long: 15, every: 4, auto: true, sound: true }
};

// ── Small helpers ────────────────────────────────────────────────────────
const p2 = n => String(n).padStart(2, '0');
const uid = () => Math.random().toString(36).slice(2, 9);
// hh:mm:ss for every timer. Counting down rounds UP (so 00:00:00 appears exactly when it ends), counting up rounds down.
const fmtHMS = (ms, round = Math.ceil) => { const s = round(ms / 1000); return p2(Math.floor(s / 3600)) + ':' + p2(Math.floor(s % 3600 / 60)) + ':' + p2(s % 60); };
const fmtIn = ms => { const s = Math.max(0, Math.ceil(ms / 1000)), h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60); return h ? `${h}h ${p2(m)}m` : m ? `${m}m ${p2(s % 60)}s` : `${s}s`; };
const clock = ts => { const d = new Date(ts); return `${p2(d.getHours())}:${p2(d.getMinutes())}:${p2(d.getSeconds())}`; };
const esc = s => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

function nextOccurrence(hhmm, now) {   // the next time the wall clock shows HH:MM
    const [h, m] = hhmm.split(':').map(Number), d = new Date(now);
    d.setHours(h, m, 0, 0);
    if (d.getTime() <= now) d.setDate(d.getDate() + 1);
    return d.getTime();
}
const phaseMs = (t, phase) => t[phase] * 60000;

function freshTimer(type) {
    const t = { id: uid(), type, status: 'idle', ...structuredClone(PRESETS[type]) };
    if (type === 'countdown') t.remaining = t.duration;
    if (type === 'stopwatch') t.elapsed = 0;
    if (type === 'pomodoro') { t.phase = 'focus'; t.cycle = 0; t.done = 0; t.remaining = phaseMs(t, 'focus'); }
    return t;
}

// ── Engine (module level, independent of what is on screen) ───────────────
let Store = null, timers = [], loaded = null;
let tickIv = null, finishTo = null, ringIv = null, saveTo = null, audioCtx = null, saveErr = false;
const faces = new Set();       // every mounted timer face (dashboard cards + pop-out windows)
const popups = new Map();      // timer id → pop-out window
const ui = { summary: null, popBtns: new Map() };

const get = id => timers.find(t => t.id === id);
const remainingOf = (t, now) => t.status === 'running' ? Math.max(0, t.endAt - now) : t.remaining;
const elapsedOf = (t, now) => t.elapsed + (t.status === 'running' ? now - t.startedAt : 0);

function save() {
    clearTimeout(saveTo);
    saveTo = setTimeout(async () => {
        try { await Store?.set(STORE_KEY, { v: 1, timers }); saveErr = false; } catch { saveErr = true; }
        ui.summary?.();
    }, 250);
}

function nextPhase(t) {
    if (t.phase === 'focus') { t.cycle++; t.done++; t.phase = t.cycle % t.every === 0 ? 'long' : 'short'; }
    else t.phase = 'focus';
}

// A timer that was running while the app was closed: settle it quietly (no ringing for the past)
function reconcile(t, now) {
    if (t.status === 'ringing') t.status = 'finished';
    if (t.status !== 'running') return;
    if (t.type === 'countdown' && t.endAt <= now) { t.remaining = 0; t.status = 'finished'; }
    else if (t.type === 'pomodoro' && t.endAt <= now) { nextPhase(t); t.status = 'paused'; t.remaining = phaseMs(t, t.phase); t.awaiting = true; }
    else if (t.type === 'alarm' && t.targetAt <= now) { if (t.repeat) t.targetAt = nextOccurrence(t.time, now); else t.status = 'finished'; }
}

function ensureLoaded() {
    return loaded ??= (async () => {
        const saved = await Store.get(STORE_KEY), now = Date.now();
        timers = (Array.isArray(saved?.timers) ? saved.timers : []).filter(t => TYPES[t?.type])
            .map(t => ({ ...freshTimer(t.type), ...t }));
        timers.forEach(t => reconcile(t, now));
        ensureTicking();
        armTimeout();
    })();
}

// ── Sound (Web Audio) ────────────────────────────────────────────────────
function ensureAudio() {
    try {
        if (!audioCtx) { audioCtx = new (window.AudioContext || window.webkitAudioContext)(); audioCtx.onstatechange = () => ui.summary?.(); }
        if (audioCtx.state === 'suspended') audioCtx.resume();
    } catch { audioCtx = null; }
}
// What is not working? level: ok · warn (will not ring audibly yet) · bad (needs attention)
function health() {
    if (saveErr) return { level: 'bad', text: 'Timers could not be saved in this browser.' };
    if (!(window.AudioContext || window.webkitAudioContext)) return { level: 'bad', text: 'This browser cannot play sound.' };
    const needsSound = timers.some(t => t.sound && t.status === 'running');
    if (needsSound && audioCtx?.state !== 'running') return { level: 'warn', text: 'Sound is locked until you click anywhere — tap here to enable alarms.' };
    return { level: 'ok', text: '' };
}
const HEALTH_DOT = { ok: 'var(--teal-400)', warn: 'var(--amber)', bad: 'var(--red)' };
function beeps(freqs, step = 0.22, len = 0.16, vol = 0.25) {
    if (!audioCtx) return;
    const t0 = audioCtx.currentTime + 0.02;
    freqs.forEach((f, i) => {
        const o = audioCtx.createOscillator(), g = audioCtx.createGain(), at = t0 + i * step;
        o.type = 'sine'; o.frequency.value = f;
        g.gain.setValueAtTime(0, at); g.gain.linearRampToValueAtTime(vol, at + 0.01); g.gain.linearRampToValueAtTime(0, at + len);
        o.connect(g); g.connect(audioCtx.destination);
        o.start(at); o.stop(at + len + 0.02);
    });
}
const alarmPattern = () => beeps([880, 880, 880, 1175]);
const chime = () => beeps([660, 880], 0.3, 0.25);

function syncRinging() {   // 'ringing' always means audible; loop until dismissed
    const any = timers.some(t => t.status === 'ringing');
    if (any && !ringIv) { alarmPattern(); ringIv = setInterval(alarmPattern, 1600); }
    else if (!any && ringIv) { clearInterval(ringIv); ringIv = null; }
}

// ── Ticking ──────────────────────────────────────────────────────────────
const ring = t => { t.status = t.sound ? 'ringing' : 'finished'; };

function phaseEnd(t, now) {
    if (t.sound) chime();
    nextPhase(t);
    const len = phaseMs(t, t.phase);
    t.remaining = len;
    if (t.auto) t.endAt = now + len;                         // keeps running into the next phase
    else { t.status = 'paused'; t.awaiting = true; }         // waits for you
}

function tick(now = Date.now()) {
    let dirty = false;
    for (const t of timers) {
        if (t.status !== 'running') continue;
        if (t.type === 'countdown' && now >= t.endAt) { t.remaining = 0; ring(t); dirty = true; }
        else if (t.type === 'alarm' && now >= t.targetAt) { ring(t); dirty = true; }
        else if (t.type === 'pomodoro' && now >= t.endAt) { phaseEnd(t, now); dirty = true; }
    }
    if (dirty) save();
    syncRinging();

    for (const [id, w] of popups) if (w.closed) popups.delete(id);
    for (const f of [...faces]) f.alive() ? f.update(now) : faces.delete(f);
    ui.popBtns.forEach((btn, id) => btn.classList.toggle('active', popups.has(id)));
    ui.summary?.();

    if (!timers.some(t => t.status === 'running' || t.status === 'ringing') && tickIv) { clearInterval(tickIv); tickIv = null; }
    armTimeout();
}
function ensureTicking() {
    if (!tickIv && timers.some(t => t.status === 'running' || t.status === 'ringing')) tickIv = setInterval(() => tick(), 200);
}
// Exact wake-up for the next ending timer (hidden tabs throttle the 200 ms interval, not a single timeout)
function armTimeout() {
    clearTimeout(finishTo);
    let next = Infinity;
    for (const t of timers) {
        if (t.status !== 'running') continue;
        if (t.type === 'alarm') next = Math.min(next, t.targetAt);
        else if (t.type !== 'stopwatch') next = Math.min(next, t.endAt);
    }
    if (next < Infinity) finishTo = setTimeout(() => tick(), Math.min(2147483647, Math.max(0, next - Date.now()) + 20));
}
function changed() { save(); ensureTicking(); tick(); }

// ── Actions ──────────────────────────────────────────────────────────────
function start(t, now) {
    t.awaiting = false;
    if (t.type === 'countdown') {
        if (t.status !== 'paused') t.remaining = t.duration;
        if (t.remaining <= 0) return;
        t.endAt = now + t.remaining;
    } else if (t.type === 'stopwatch') t.startedAt = now;
    else if (t.type === 'alarm') t.targetAt = nextOccurrence(t.time, now);
    else if (t.type === 'pomodoro') t.endAt = now + t.remaining;
    t.status = 'running';
}
function pause(t, now) {
    if (t.type === 'stopwatch') t.elapsed += now - t.startedAt;
    else if (t.type === 'countdown' || t.type === 'pomodoro') t.remaining = Math.max(0, t.endAt - now);
    else return;
    t.status = 'paused';
}
function stop(t, now) {
    const wasDone = t.status === 'ringing' || t.status === 'finished';
    t.awaiting = false;
    if (t.type === 'countdown') { t.remaining = t.duration; t.status = 'idle'; }
    else if (t.type === 'stopwatch') { t.elapsed = 0; t.status = 'idle'; }
    else if (t.type === 'pomodoro') { t.phase = 'focus'; t.cycle = 0; t.done = 0; t.remaining = phaseMs(t, 'focus'); t.status = 'idle'; }
    else if (t.type === 'alarm') {
        if (wasDone && t.repeat) { t.targetAt = nextOccurrence(t.time, now); t.status = 'running'; }   // daily alarm: dismiss = arm for tomorrow
        else t.status = 'idle';
    }
}
function skip(t, now) {   // pomodoro: jump to the next phase
    if (t.type !== 'pomodoro' || t.status === 'idle') return;
    const wasRunning = t.status === 'running';
    nextPhase(t);
    t.remaining = phaseMs(t, t.phase);
    t.awaiting = !wasRunning;
    if (wasRunning) t.endAt = now + t.remaining;
}

function act(id, what) {
    ensureAudio();   // browsers only allow sound after a click — every action is one
    const t = get(id), now = Date.now();
    if (!t) return;
    const canStart = t.status === 'idle' || t.status === 'paused';
    if (what === 'play') { if (canStart) start(t, now); }
    else if (what === 'pause') { if (t.status === 'running') pause(t, now); }
    else if (what === 'toggle') { if (t.status === 'running') pause(t, now); else if (canStart) start(t, now); }   // Space bar
    else if (what === 'stop') stop(t, now);
    else if (what === 'skip') skip(t, now);
    changed();
}

// Settings can change while a timer runs. Length changes shift the time that is left by the difference.
function applySettings(t, v) {
    t.title = v.title || TYPES[t.type];
    if ('sound' in v) { t.sound = v.sound; if (!t.sound && t.status === 'ringing') t.status = 'finished'; }
    const now = Date.now();
    if (t.type === 'countdown') {
        const delta = v.duration - t.duration;
        t.duration = v.duration;
        if (t.status === 'idle') t.remaining = t.duration;
        else if (t.status === 'running') t.endAt += delta;
        else if (t.status === 'paused') t.remaining = Math.max(0, t.remaining + delta);
    } else if (t.type === 'alarm') {
        t.time = v.time; t.repeat = v.repeat;
        if (t.status === 'running') t.targetAt = nextOccurrence(t.time, now);
    } else if (t.type === 'pomodoro') {
        const old = phaseMs(t, t.phase);
        Object.assign(t, { focus: v.focus, short: v.short, long: v.long, every: v.every, auto: v.auto });
        const delta = phaseMs(t, t.phase) - old;
        if (t.status === 'idle') t.remaining = phaseMs(t, t.phase);
        else if (t.status === 'running') t.endAt += delta;
        else if (t.status === 'paused') t.remaining = Math.max(0, t.remaining + delta);
    }
}

// ── What a timer looks like right now ─────────────────────────────────────
function view(t, now) {
    const st = t.status, done = st === 'ringing' || st === 'finished', running = st === 'running';
    // Every timer has the same three buttons. canPlay/canPause = can be pressed; running/paused = which one lights up.
    const buttons = { canPlay: st === 'idle' || st === 'paused', canPause: running, running, paused: st === 'paused',
        playLabel: running ? 'Running' : st === 'paused' ? 'Resume' : 'Start', canStop: st !== 'idle', editable: true };
    if (t.type === 'countdown') {
        const rem = remainingOf(t, now);
        return { ...buttons, time: fmtHMS(rem), progress: t.duration ? 1 - rem / t.duration : 0, stopLabel: done ? 'Dismiss' : 'Stop',
            sub: running ? `Ends at ${clock(t.endAt)}` : st === 'paused' ? 'Paused' : st === 'ringing' ? "Time's up!" : st === 'finished' ? "Time's up" : `Set to ${fmtHMS(t.duration)}` };
    }
    if (t.type === 'stopwatch') {
        return { ...buttons, time: fmtHMS(elapsedOf(t, now), Math.floor), progress: null, stopLabel: 'Reset', editable: false,
            sub: running ? 'Running' : st === 'paused' ? 'Paused' : 'Ready' };
    }
    if (t.type === 'alarm') {
        const day = running && new Date(t.targetAt).getDate() === new Date(now).getDate() ? 'today' : 'tomorrow';
        return { ...buttons, time: t.time, progress: null, canPlay: st === 'idle', canPause: false, paused: false,
            playLabel: running ? 'Armed' : 'Arm', stopLabel: running ? 'Disarm' : 'Dismiss',
            sub: running ? `Rings ${day} · in ${fmtIn(t.targetAt - now)}${t.repeat ? ' · daily' : ''}` : st === 'ringing' ? 'Alarm!' : st === 'finished' ? 'Alarm time reached' : `Not armed${t.repeat ? ' · daily' : ''}` };
    }
    const rem = remainingOf(t, now), len = phaseMs(t, t.phase), count = t.done ? ` · ✓ ${t.done}` : '';
    return { ...buttons, time: fmtHMS(rem), progress: len ? 1 - rem / len : 0, phase: t.phase, paused: st === 'paused' && !t.awaiting,
        stopLabel: 'Reset', canSkip: st !== 'idle',
        sub: st === 'idle' ? `Ready · ${t.focus} min focus${count}`
            : t.awaiting ? `Up next: ${PHASES[t.phase]} — press Resume${count}`
            : `${PHASES[t.phase]} · session ${(t.cycle % t.every) + 1} of ${t.every}${count}` };
}

const CTL = {   // standard transport icons
    play: '<svg viewBox="0 0 24 24" fill="currentColor" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><polygon points="7 4 20 12 7 20"/></svg>',
    pause: '<svg viewBox="0 0 24 24" fill="currentColor" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><rect x="6" y="4" width="4" height="16"/><rect x="14" y="4" width="4" height="16"/></svg>',
    stop: '<svg viewBox="0 0 24 24" fill="currentColor" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><rect x="5" y="5" width="14" height="14" rx="1"/></svg>',
    skip: '<svg viewBox="0 0 24 24" fill="currentColor" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><polygon points="5 4 15 12 5 20"/><line x1="19" y1="5" x2="19" y2="19"/></svg>'
};

// ── Click-to-rename: swaps a title for an input (Enter / click away = save, Esc = cancel). Works in any window.
function editTitle(doc, el, id) {
    const t = get(id);
    if (!t || el.hidden) return;
    const input = doc.createElement('input');
    input.type = 'text'; input.className = 'input timer-title-input'; input.maxLength = 60; input.value = t.title;
    el.hidden = true;
    el.after(input);
    input.focus(); input.select();
    let done = false;
    const finish = commit => {
        if (done) return;
        done = true;
        const name = input.value.trim();
        if (commit && name && name !== t.title) { t.title = name; save(); }
        input.remove();
        el.textContent = t.title; el.hidden = false;
        tick();
    };
    input.onblur = () => finish(true);
    input.onkeydown = e => { if (e.key === 'Enter') finish(true); else if (e.key === 'Escape') finish(false); };
}

// ── A timer face: time, status line, progress, controls (used by cards AND pop-out windows) ──
function makeFace(doc, id, onUpdate, onTimeClick) {
    const type = get(id).type;
    const el = doc.createElement('div');
    el.className = 'timer-face';
    el.innerHTML = '<div class="timer-time"></div><div class="timer-sub"></div><div class="timer-progress"><span></span></div><div class="timer-controls"></div>';
    const q = s => el.querySelector(s), time = q('.timer-time'), sub = q('.timer-sub'), bar = q('.timer-progress'), ctl = q('.timer-controls');
    const button = (cls, what, icon) => {
        const b = doc.createElement('button');
        b.type = 'button'; b.className = 'ctl-btn ' + cls; b.dataset.act = what; b.innerHTML = icon;
        b.onclick = () => act(id, what);
        ctl.appendChild(b);
        return b;
    };
    const playBtn = button('', 'play', CTL.play), pauseBtn = button('', 'pause', CTL.pause), stopBtn = button('', 'stop', CTL.stop);
    const skipBtn = type === 'pomodoro' ? button('', 'skip', CTL.skip) : null;
    const label = (b, text) => { b.title = text; b.setAttribute('aria-label', text); };
    if (skipBtn) label(skipBtn, 'Skip');
    time.onclick = () => { if (onTimeClick && view(get(id), Date.now()).editable) onTimeClick(); };

    const face = {
        el,
        alive: () => el.isConnected && !doc.defaultView?.closed,
        update(now) {
            const t = get(id);
            if (!t) return;
            const v = view(t, now);
            time.textContent = v.time; sub.textContent = v.sub;
            bar.hidden = v.progress == null;
            if (v.progress != null) bar.style.setProperty('--p', Math.min(1, Math.max(0, v.progress)).toFixed(4));
            el.className = 'timer-face' + (v.phase ? ' phase-' + v.phase : '') + (t.status === 'ringing' ? ' ringing' : '') + (t.status === 'finished' ? ' done' : '');
            const set = (b, text, enabled, on) => { label(b, text); b.disabled = !enabled && !on; b.classList.toggle('on', !!on); };   // lit = the current state
            set(playBtn, v.playLabel, v.canPlay, v.running);
            set(pauseBtn, 'Pause', v.canPause, v.paused);
            set(stopBtn, v.stopLabel, v.canStop, false);
            time.classList.toggle('editable', !!onTimeClick && !!v.editable);
            time.title = onTimeClick && v.editable ? 'Click to change the time' : '';
            if (skipBtn) skipBtn.disabled = !v.canSkip;
            onUpdate?.(t, v);
        }
    };
    faces.add(face);
    face.update(Date.now());
    return el;
}

// ── Pop-out window ────────────────────────────────────────────────────────
function popOut(id, toast) {
    const t = get(id);
    if (!t) return;
    const existing = popups.get(id);
    if (existing && !existing.closed) { existing.focus(); return; }
    const w = window.open('', 'timer-' + id, 'popup=yes,width=520,height=380');
    if (!w) { toast('Pop-up blocked — allow pop-ups for this site and try again.'); return; }
    popups.set(id, w);

    const css = [...document.querySelectorAll('link[rel="stylesheet"]')].find(l => /styles\.css/.test(l.href))?.href || 'styles.css';
    w.document.open();
    w.document.write(`<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>${esc(t.title)}</title><link rel="stylesheet" href="${esc(css)}"></head><body class="popout"></body></html>`);
    w.document.close();

    const doc = w.document, body = doc.body;
    body.innerHTML = `<div class="popout-bar"><span class="popout-title"></span><button type="button" class="btn-icon" title="Full screen (F)">
        <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M8 3H5a2 2 0 0 0-2 2v3M21 8V5a2 2 0 0 0-2-2h-3M3 16v3a2 2 0 0 0 2 2h3M16 21h3a2 2 0 0 0 2-2v-3"/></svg></button></div>`;
    const titleEl = body.querySelector('.popout-title'), fsBtn = body.querySelector('.btn-icon');
    body.appendChild(makeFace(doc, id, (tm, v) => { titleEl.textContent = tm.title; doc.title = `${v.time} · ${tm.title}`; }));
    titleEl.title = 'Click to rename';
    titleEl.onclick = () => editTitle(doc, titleEl, id);

    const toggleFs = () => { doc.fullscreenElement ? doc.exitFullscreen?.() : doc.documentElement.requestFullscreen?.().catch(() => {}); };
    fsBtn.onclick = toggleFs;
    body.querySelector('.timer-time').ondblclick = toggleFs;
    doc.addEventListener('keydown', e => {
        if (e.target.tagName === 'INPUT') return;   // typing a new name
        if (e.code === 'Space') {
            e.preventDefault();
            const tm = get(id), v = tm && view(tm, Date.now());
            if (v?.canPlay || v?.canPause) act(id, 'toggle'); else if (v?.canStop) act(id, 'stop');
        } else if (e.key.toLowerCase() === 'f') toggleFs();
    });
    doc.addEventListener('fullscreenchange', () => body.classList.toggle('is-fullscreen', !!doc.fullscreenElement));

    // The pop-out drives itself too: it is on screen, so the browser doesn't throttle it like a background tab
    const iv = w.setInterval(() => tick(), 250);
    w.addEventListener('pagehide', () => { w.clearInterval(iv); popups.delete(id); });
    tick();
}
if (typeof window !== 'undefined') {
    window.addEventListener('pagehide', () => popups.forEach(w => w.close()));   // never leave orphaned windows behind
    ['click', 'keydown'].forEach(ev => document.addEventListener(ev, ensureAudio));   // unlock sound on any interaction
}

// ── Dashboard ─────────────────────────────────────────────────────────────
const ICON = {
    pop: '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>',
    set: '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09a1.65 1.65 0 0 0-1-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09a1.65 1.65 0 0 0 1.51-1 1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33h0a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51h0a1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82v0a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>',
    del: '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6M14 11v6"/><path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/></svg>'
};

const TEMPLATE = `
<div class="editor-panel fill">
  <div class="banner">
    <div class="banner-main"><div class="toolbar-group">
      <button class="btn btn-outline btn-sm" data-add="countdown">+ Countdown</button>
      <button class="btn btn-outline btn-sm" data-add="stopwatch">+ Stopwatch</button>
      <button class="btn btn-outline btn-sm" data-add="alarm">+ Alarm</button>
      <button class="btn btn-outline btn-sm" data-add="pomodoro">+ Pomodoro</button>
    </div></div>
    <div class="banner-actions"><button class="btn-icon" id="tm-status" aria-label="Status"><span id="tm-dot" style="display:block;width:10px;height:10px;border-radius:50%;background:var(--teal-400)"></span></button></div>
  </div>
  <div class="card-grid" id="tm-grid"></div>
</div>
<div class="toast" id="tm-toast"></div>
<div class="modal-overlay" id="tm-modal">
  <div class="modal-card">
    <div class="modal-header"><span id="tm-modal-title"></span><button class="modal-close" id="tm-modal-x">&times;</button></div>
    <div class="modal-body" id="tm-modal-body"></div>
    <div class="modal-footer">
      <button class="btn btn-outline" id="tm-test" hidden>Test sound</button>
      <button class="btn btn-outline" id="tm-cancel">Cancel</button>
      <button class="btn btn-primary" id="tm-save">Save</button>
    </div>
  </div>
</div>`;

export async function render(root, App) {
    Store = App.Store;
    await ensureLoaded();
    root.innerHTML = TEMPLATE;
    const $ = s => root.querySelector(s);
    const grid = $('#tm-grid'), modal = $('#tm-modal'), toastEl = $('#tm-toast');

    let toastTo = null;
    const toast = (msg, err) => { toastEl.textContent = msg; toastEl.classList.toggle('error', !!err); toastEl.classList.add('show'); clearTimeout(toastTo); toastTo = setTimeout(() => toastEl.classList.remove('show'), 3500); };

    const summaryText = () => {
        const ringing = timers.filter(t => t.status === 'ringing').length, running = timers.filter(t => t.status === 'running').length;
        return ringing ? `${ringing} ringing!` : running ? `${running} running` : 'No timers running';
    };
    ui.summary = () => {   // the dot shows if something is not working; tap it for details
        if (!root.isConnected) return;
        const h = health();
        $('#tm-dot').style.background = HEALTH_DOT[h.level];
        $('#tm-status').title = h.text || summaryText();
    };
    $('#tm-status').onclick = () => {
        ensureAudio();   // this click also unlocks sound
        setTimeout(() => { ui.summary(); const h = health(); toast(h.text || summaryText(), h.level === 'bad'); }, 150);
    };

    // ── cards ──
    function makeCard(t) {
        const card = document.createElement('div');
        card.className = 'timer-card';
        card.innerHTML = `<div class="timer-card-head"><span class="timer-card-title"></span><span class="chip"></span>
            <div class="timer-card-actions">
              <button type="button" class="btn-icon" data-pop title="Open in separate window">${ICON.pop}</button>
              <button type="button" class="btn-icon" data-set title="Settings">${ICON.set}</button>
              <button type="button" class="btn-icon danger" data-del title="Delete timer">${ICON.del}</button>
            </div></div>`;
        const titleEl = card.querySelector('.timer-card-title');
        titleEl.textContent = t.title; titleEl.title = 'Click to rename';
        titleEl.onclick = () => editTitle(document, titleEl, t.id);
        card.querySelector('.chip').textContent = TYPES[t.type];
        const pop = card.querySelector('[data-pop]');
        ui.popBtns.set(t.id, pop);
        pop.onclick = () => { popOut(t.id, toast); pop.classList.toggle('active', popups.has(t.id)); };
        card.querySelector('[data-set]').onclick = () => openSettings(t, false);
        const timeField = { countdown: 'tm-f-m', alarm: 'tm-f-time', pomodoro: 'tm-f-focus' }[t.type];
        card.querySelector('[data-del]').onclick = () => {
            if (!confirm(`Delete "${t.title}"?`)) return;
            popups.get(t.id)?.close(); popups.delete(t.id);
            timers = timers.filter(x => x.id !== t.id);
            renderGrid(); changed();
        };
        const syncTitle = tm => { if (!titleEl.hidden && titleEl.textContent !== tm.title) titleEl.textContent = tm.title; };   // renamed elsewhere (pop-out)?
        card.appendChild(makeFace(document, t.id, syncTitle, () => openSettings(t, false, timeField)));
        return card;
    }
    function renderGrid() {
        ui.popBtns.clear();
        grid.innerHTML = '';
        if (!timers.length) grid.innerHTML = '<div class="hint">No timers yet — add one with the buttons above.</div>';
        timers.forEach(t => grid.appendChild(makeCard(t)));
    }

    // ── settings dialog (works for new and running timers) ──
    let editing = null, isNew = false;
    const field = (label, inner) => `<div class="field"><label class="field-label">${label}</label>${inner}</div>`;
    const num = (id, min, max) => `<input type="number" class="input" id="${id}" min="${min}" max="${max}">`;
    const col = (inner, hint) => `<label class="field-col">${inner}<span class="field-hint">${hint}</span></label>`;
    const check = (id, text) => `<label class="check"><input type="checkbox" id="${id}"><span>${text}</span></label>`;

    function openSettings(t, creating, focusId = 'tm-f-title') {
        editing = t; isNew = creating;
        $('#tm-modal-title').textContent = creating ? `New ${TYPES[t.type].toLowerCase()}` : `${TYPES[t.type]} settings`;
        let html = field('Title', '<input type="text" class="input" id="tm-f-title" maxlength="60">');
        if (t.type === 'countdown') {
            html += field('Duration', `<div class="field-row">${col(num('tm-f-h', 0, 99), 'hours')}${col(num('tm-f-m', 0, 59), 'minutes')}${col(num('tm-f-s', 0, 59), 'seconds')}</div>`);
        } else if (t.type === 'alarm') {
            html += field('Alarm time', '<input type="time" class="input" id="tm-f-time">') + check('tm-f-repeat', 'Repeat daily');
        } else if (t.type === 'pomodoro') {
            html += field('Lengths (minutes)', `<div class="field-row">${col(num('tm-f-focus', 1, 180), 'focus')}${col(num('tm-f-short', 1, 60), 'short break')}${col(num('tm-f-long', 1, 120), 'long break')}</div>`)
                + field('Long break after', `<div class="field-row">${col(num('tm-f-every', 1, 12), 'focus sessions')}</div>`)
                + check('tm-f-auto', 'Start the next phase automatically');
        }
        if (t.type !== 'stopwatch') html += check('tm-f-sound', 'Sound alarm');
        html += '<div class="form-error" id="tm-f-error"></div>';
        $('#tm-modal-body').innerHTML = html;
        $('#tm-test').hidden = t.type === 'stopwatch';

        const set = (id, v) => { const e = $('#' + id); if (e) e.value = v; };
        const tick_ = (id, v) => { const e = $('#' + id); if (e) e.checked = v; };
        set('tm-f-title', t.title);
        if (t.type === 'countdown') { const s = Math.round(t.duration / 1000); set('tm-f-h', Math.floor(s / 3600)); set('tm-f-m', Math.floor(s % 3600 / 60)); set('tm-f-s', s % 60); }
        if (t.type === 'alarm') { set('tm-f-time', t.time); tick_('tm-f-repeat', t.repeat); }
        if (t.type === 'pomodoro') { set('tm-f-focus', t.focus); set('tm-f-short', t.short); set('tm-f-long', t.long); set('tm-f-every', t.every); tick_('tm-f-auto', t.auto); }
        tick_('tm-f-sound', t.sound);
        modal.classList.add('open');
        const target = $('#' + focusId) || $('#tm-f-title');
        target.focus(); target.select?.();
    }

    function readForm() {
        const t = editing, n = id => parseInt($('#' + id).value, 10), bad = msg => { $('#tm-f-error').textContent = msg; return null; };
        const v = { title: $('#tm-f-title').value.trim() };
        if ($('#tm-f-sound')) v.sound = $('#tm-f-sound').checked;
        if (t.type === 'countdown') {
            const h = n('tm-f-h') || 0, m = n('tm-f-m') || 0, s = n('tm-f-s') || 0;
            v.duration = ((h * 60 + m) * 60 + s) * 1000;
            if (v.duration < 1000) return bad('Duration must be at least 1 second.');
        } else if (t.type === 'alarm') {
            v.time = $('#tm-f-time').value; v.repeat = $('#tm-f-repeat').checked;
            if (!/^\d{2}:\d{2}$/.test(v.time)) return bad('Pick the alarm time.');
        } else if (t.type === 'pomodoro') {
            Object.assign(v, { focus: n('tm-f-focus'), short: n('tm-f-short'), long: n('tm-f-long'), every: n('tm-f-every'), auto: $('#tm-f-auto').checked });
            if (![v.focus, v.short, v.long, v.every].every(x => x >= 1)) return bad('All lengths must be at least 1.');
        }
        return v;
    }

    const closeModal = () => { modal.classList.remove('open'); editing = null; };
    $('#tm-save').onclick = () => {
        ensureAudio();
        const v = readForm();
        if (!v) return;
        if (isNew) timers.push(editing);
        applySettings(editing, v);
        closeModal(); renderGrid(); changed();
    };
    $('#tm-cancel').onclick = closeModal;
    $('#tm-modal-x').onclick = closeModal;
    modal.onclick = e => { if (e.target === modal) closeModal(); };
    $('#tm-modal-body').addEventListener('keydown', e => { if (e.key === 'Enter') $('#tm-save').click(); });
    $('#tm-test').onclick = () => { ensureAudio(); setTimeout(chime, 50); };

    root.querySelectorAll('[data-add]').forEach(b => { b.onclick = () => openSettings(freshTimer(b.dataset.add), true); });

    renderGrid();
    ui.summary();
    ensureTicking();
    tick();
}
