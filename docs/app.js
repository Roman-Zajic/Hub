// Cache busting: GitHub Pages caches files for ~10 minutes. Bump "?v=" in index.html (styles.css and app.js) after a deploy;
// every other file (config, modules) inherits the same version from this file's own URL.
const V = new URL(import.meta.url).search;
const { APP_CONFIG } = await import('./config.js' + V);

// 1. INDEXED-DB KEY-VALUE STORE (one connection, opened once)
let dbPromise = null;
const Store = {
    getDb() {
        return dbPromise ??= new Promise((resolve, reject) => {
            const req = indexedDB.open('AppDB', 1);
            req.onupgradeneeded = e => e.target.result.createObjectStore('kv_store');
            req.onsuccess = e => {
                const db = e.target.result;
                db.onversionchange = () => { db.close(); dbPromise = null; };   // another tab upgrades/deletes: reconnect on next use
                db.onclose = () => { dbPromise = null; };
                resolve(db);
            };
            req.onerror = e => { dbPromise = null; reject(e.target.error); };
        });
    },
    async get(key) {
        const db = await this.getDb();
        return new Promise((resolve, reject) => {
            const req = db.transaction('kv_store', 'readonly').objectStore('kv_store').get(key);
            req.onsuccess = e => resolve(e.target.result);
            req.onerror = e => reject(e.target.error);
        });
    },
    async set(key, value) {
        const db = await this.getDb();
        return new Promise((resolve, reject) => {
            const req = db.transaction('kv_store', 'readwrite').objectStore('kv_store').put(value, key);
            req.onsuccess = () => resolve();
            req.onerror = e => reject(e.target.error);
        });
    }
};

// ── Shared helpers for all modules: App.esc · App.pad · App.toast · App.loadScript ──
const esc = s => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
const pad = n => String(n).padStart(2, '0');
let toastTimer = null;
function toast(msg, error) {   // one toast for the whole app (#app-toast in index.html)
    const el = document.getElementById('app-toast');
    if (!el) return;
    el.textContent = msg;
    el.classList.toggle('error', !!error);
    el.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.remove('show'), error ? 5500 : 3000);
}
const scripts = {};
const loadScript = src => scripts[src] ??= new Promise((ok, fail) => {   // each library is loaded once; a failed load can be retried
    const s = document.createElement('script');
    s.src = src; s.onload = ok;
    s.onerror = () => { delete scripts[src]; s.remove(); fail(new Error('Could not load ' + src)); };
    document.head.appendChild(s);
});

// 2. GLOBAL APP OBJECT
window.App = {
    config: APP_CONFIG,
    Store: Store,
    esc, pad, toast, loadScript,
    
    Storage: {
        Local: {
            // The working directory is chosen ONCE in Settings and remembered (IndexedDB). Modules never open a folder picker themselves.
            handle: null,
            async init(interactive = true) {
                if (!('showDirectoryPicker' in window)) throw new Error("Local folder not supported here.");
                if (!this.handle) this.handle = await App.Store.get('local_folder_handle');
                if (!this.handle) throw new Error('NO_FOLDER');
                let perm = await this.handle.queryPermission({ mode: 'readwrite' });
                if (perm !== 'granted') {
                    if (!interactive) throw new Error('NEEDS_PERMISSION');   // browsers only ask after a click
                    perm = await this.handle.requestPermission({ mode: 'readwrite' });
                    if (perm !== 'granted') throw new Error("Permission denied.");
                }
                return this.handle;
            },
            // A subfolder of the working directory (created on first use). dir('') = the working directory itself.
            async dir(sub = '', interactive = true) {
                const root = await this.init(interactive);
                return sub ? root.getDirectoryHandle(sub, { create: true }) : root;
            }
        },
    }
};

// 3. SETTINGS MODAL LOGIC
async function initSettings() {
    const overlay = document.getElementById('settings-overlay');
    const btnOpen = document.getElementById('settings-btn');
    const btnClose = document.getElementById('settings-close');
    const btnSave = document.getElementById('settings-save');
    const btnFolder = document.getElementById('set-local-folder');
    const modBox = document.getElementById('set-modules');
    const modBtn = document.getElementById('set-modules-btn');
    const modMenu = document.getElementById('set-modules-menu');
    const modLabelEl = document.getElementById('set-modules-label');

    // Modules dropdown: one checkbox per module; the choice is stored in this browser only (= per device)
    const pickedModules = () => [...modMenu.querySelectorAll('input:checked')].map(i => i.value);
    function updateModulesLabel() {
        const n = pickedModules().length, total = APP_CONFIG.modules.length;
        modLabelEl.textContent = n === total ? 'All modules' : n ? `${n} of ${total} selected` : 'None selected';
    }
    function renderModulesMenu() {
        modMenu.innerHTML = '';
        APP_CONFIG.modules.forEach(name => {
            const row = document.createElement('label');
            row.className = 'multi-select-item';
            row.innerHTML = `<input type="checkbox" value="${name}"><span>${modLabel(name)}</span>`;
            const box = row.querySelector('input');
            box.checked = enabledModules.includes(name);
            box.addEventListener('change', updateModulesLabel);
            modMenu.appendChild(row);
        });
        updateModulesLabel();
    }
    modBtn.addEventListener('click', e => { e.stopPropagation(); modBox.classList.toggle('open'); });
    document.addEventListener('click', e => { if (!modBox.contains(e.target)) modBox.classList.remove('open'); });

    btnOpen.addEventListener('click', async () => {
        document.getElementById('set-gh-repo').value = await App.Store.get('gh_repo') || '';
        document.getElementById('set-gh-token').value = await App.Store.get('gh_token') || '';
        const savedFolder = await App.Store.get('gh_folder');
        document.getElementById('set-gh-folder').value = savedFolder !== undefined && savedFolder !== null ? savedFolder : '';
        const handle = await App.Store.get('local_folder_handle');
        document.getElementById('set-local-status').textContent = handle ? `Current: ${handle.name}` : 'No folder selected';
        renderModulesMenu();
        modBox.classList.remove('open');
        overlay.classList.add('open');
    });

    btnClose.addEventListener('click', () => overlay.classList.remove('open'));

    btnSave.addEventListener('click', async () => {
        const picked = pickedModules();
        if (!picked.length) { alert('Select at least one module for this device.'); return; }
        await App.Store.set('gh_repo', document.getElementById('set-gh-repo').value.trim());
        await App.Store.set('gh_token', document.getElementById('set-gh-token').value.trim());
        await App.Store.set('gh_folder', document.getElementById('set-gh-folder').value.trim());
        await App.Store.set('enabled_modules', picked);
        enabledModules = APP_CONFIG.modules.filter(m => picked.includes(m));
        buildSidebar();
        overlay.classList.remove('open');
        const current = window.location.hash.replace('#', '');
        if (enabledModules.includes(current)) handleNavigation();           // re-render with the new credentials
        else window.location.hash = enabledModules[0];                      // this module is off on this device → hashchange navigates
    });

    btnFolder.addEventListener('click', async () => {
        try {
            const handle = await window.showDirectoryPicker({ mode: 'readwrite' });
            await App.Store.set('local_folder_handle', handle);
            document.getElementById('set-local-status').textContent = `Current: ${handle.name}`;
            App.Storage.Local.handle = handle;
            window.dispatchEvent(new Event('app:folder-changed'));   // background modules (transcript) pick the folder up at once
        } catch (e) { console.log("Folder selection cancelled."); }
    });
}

// 4. ROUTER & APP INIT
const els = {
    root: document.getElementById('app-root'),
    sidebar: document.getElementById('sidebar'),
    brandNameDesktop: document.getElementById('brand-name-desktop'),
    moduleTitle: document.getElementById('active-module-title'),
    footerModule: document.getElementById('footer-module-name'),
    mobileBtn: document.getElementById('mobile-menu-btn')
};

const loadedModules = {};
const loadModule = name => loadedModules[name] ??= import(`./modules/${name}.js${V}`).catch(err => { delete loadedModules[name]; throw err; });
let currentModule = null;   // name of the module that is on screen
const modLabel = name => name.split('-').map(w => w.charAt(0).toUpperCase() + w.slice(1)).join(' ');

// Modules switched on for THIS device (stored in this browser's IndexedDB). Nothing stored yet → all of them.
let enabledModules = [...APP_CONFIG.modules];
async function loadEnabledModules() {
    const saved = await Store.get('enabled_modules');
    const list = Array.isArray(saved) ? APP_CONFIG.modules.filter(m => saved.includes(m)) : [];
    return list.length ? list : [...APP_CONFIG.modules];
}
function buildSidebar() {
    els.sidebar.innerHTML = '';
    enabledModules.forEach(modName => {
        const link = document.createElement('a');
        link.href = `#${modName}`;
        link.className = 'nav-item';
        link.textContent = modLabel(modName);
        link.dataset.module = modName;
        els.sidebar.appendChild(link);
    });
}

async function initApp() {
    els.brandNameDesktop.textContent = APP_CONFIG.brandName;

    enabledModules = await loadEnabledModules();
    buildSidebar();

    window.addEventListener('hashchange', handleNavigation);
    els.mobileBtn.addEventListener('click', () => els.sidebar.classList.toggle('open'));

    initSettings();

    // Modules that keep working in the background (e.g. the all-day transcript): load them now and call their boot()
    (APP_CONFIG.background || []).filter(n => enabledModules.includes(n)).forEach(n =>
        loadModule(n).then(m => m.boot?.(App)).catch(err => console.error(`Background module '${n}' failed`, err)));

    // empty / unknown / switched-off module in the URL (e.g. the old #private-notes) → default module
    const current = window.location.hash.replace('#', '');
    if (!enabledModules.includes(current)) window.location.hash = enabledModules.includes(APP_CONFIG.defaultModule) ? APP_CONFIG.defaultModule : enabledModules[0];
    else handleNavigation();
}

async function handleNavigation() {
    const hash = window.location.hash.replace('#', '');
    if (!enabledModules.includes(hash)) return;

    document.querySelectorAll('.nav-item').forEach(el => el.classList.toggle('active', el.dataset.module === hash));
    els.sidebar.classList.remove('open');

    // The module that is leaving gets a chance to save what is pending and remove its listeners
    if (currentModule) {
        try { await loadedModules[currentModule]?.then?.(m => m.unmount?.()); }
        catch (error) { console.error(`unmount failed: ${currentModule}`, error); }
        currentModule = null;
    }
    els.root.innerHTML = '<div style="padding: 24px;">Loading...</div>';

    try {
        const module = await loadModule(hash);
        const moduleTitle = module.meta?.title || modLabel(hash);
        els.moduleTitle.textContent = moduleTitle;
        els.footerModule.textContent = moduleTitle;

        els.root.innerHTML = '';
        currentModule = hash;
        if (module.render) await module.render(els.root, App);
    } catch (error) {
        console.error(`Failed to load module: ${hash}`, error);
        els.root.innerHTML = `<div style="padding: 24px; color: var(--red);">Could not load module '${esc(hash)}': ${esc(error.message || error)}<br><small>Details are in the browser console (F12).</small></div>`;
    }
}

initApp();