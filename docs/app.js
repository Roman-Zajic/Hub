import { APP_CONFIG } from './config.js';

// 1. INDEXED-DB KEY-VALUE STORE
const Store = {
    async getDb() {
        return new Promise((resolve, reject) => {
            const req = indexedDB.open('AppDB', 1);
            req.onupgradeneeded = e => e.target.result.createObjectStore('kv_store');
            req.onsuccess = e => resolve(e.target.result);
            req.onerror = e => reject(e.target.error);
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

// 2. GLOBAL APP OBJECT
window.App = {
    config: APP_CONFIG,
    Store: Store,
    
    Storage: {
        Local: {
            handle: null,
            async init() {
                if (!('showDirectoryPicker' in window)) throw new Error("Local folder not supported here.");
                if (!this.handle) this.handle = await App.Store.get('local_folder_handle');
                if (this.handle) {
                    const perm = await this.handle.queryPermission({ mode: 'readwrite' });
                    if (perm !== 'granted') {
                        const request = await this.handle.requestPermission({ mode: 'readwrite' });
                        if (request !== 'granted') throw new Error("Permission denied.");
                    }
                    return this.handle;
                }
                this.handle = await window.showDirectoryPicker();
                await App.Store.set('local_folder_handle', this.handle);
                return this.handle;
            },
            async listFiles() {
                await this.init();
                const files = [];
                for await (const entry of this.handle.values()) {
                    if (entry.kind === 'file' && entry.name.endsWith('.md')) files.push(entry.name);
                }
                return files;
            },
            async readFile(filename) {
                await this.init();
                const fileHandle = await this.handle.getFileHandle(filename);
                const file = await fileHandle.getFile();
                return await file.text();
            },
            async saveFile(filename, content) {
                await this.init();
                const fileHandle = await this.handle.getFileHandle(filename, { create: true });
                const writable = await fileHandle.createWritable();
                await writable.write(content);
                await writable.close();
            },
            async deleteFile(filename) {
                await this.init();
                await this.handle.removeEntry(filename);
            }
        },
        GitHub: {
            async getFolder() {
                const folder = await App.Store.get('gh_folder');
                let f = (folder !== undefined && folder !== null) ? folder.trim() : '';
                return f.replace(/^\/+|\/+$/g, '');
            },
            async getPath(filepath) {
                const folder = await this.getFolder();
                const cleanFile = filepath.replace(/^\/+/, '');
                if (folder && (cleanFile === folder || cleanFile.startsWith(folder + '/'))) {
                    return cleanFile;
                }
                return folder ? `${folder}/${cleanFile}` : cleanFile;
            },
            async getCreds() {
                const token = await App.Store.get('gh_token');
                const repo = await App.Store.get('gh_repo');
                if (!token || !repo) throw new Error("GitHub credentials not configured.");
                return { token, repo };
            },
            encodeBase64(str) { return btoa(unescape(encodeURIComponent(str))); },
            decodeBase64(str) { return decodeURIComponent(escape(atob(str))); },
            async apiCall(endpoint, method = 'GET', body = null) {
                const { token, repo } = await this.getCreds();
                const cleanEndpoint = endpoint.replace(/^\/+/, '');
                const url = cleanEndpoint 
                    ? `https://api.github.com/repos/${repo}/contents/${cleanEndpoint}`
                    : `https://api.github.com/repos/${repo}/contents`;

                const options = {
                    method,
                    headers: { 'Authorization': `Bearer ${token}`, 'Accept': 'application/vnd.github.v3+json', 'Content-Type': 'application/json' }
                };
                if (body) options.body = JSON.stringify(body);
                const res = await fetch(url, options);
                if (!res.ok) throw new Error(`GitHub API Error: ${res.statusText}`);
                return await res.json();
            },
            async listFiles() {
                try {
                    const folder = await this.getFolder();
                    let data;
                    try {
                        data = await this.apiCall(folder);
                    } catch (e) {
                        return [];
                    }
                    const files = [];
                    
                    const walk = async (items, currentPath = '') => {
                        for (const item of items) {
                            if (item.type === 'file' && item.name.endsWith('.md')) {
                                const relPath = currentPath ? `${currentPath}/${item.name}` : item.name;
                                files.push(relPath);
                            } else if (item.type === 'dir') {
                                try {
                                    const subData = await this.apiCall(item.path);
                                    if (Array.isArray(subData)) {
                                        const nextPath = currentPath ? `${currentPath}/${item.name}` : item.name;
                                        await walk(subData, nextPath);
                                    }
                                } catch (e) {}
                            }
                        }
                    };
                    
                    if (Array.isArray(data)) {
                        await walk(data, ''); // Pass empty string so relative paths are correct
                    }
                    return files;
                } catch (e) { return []; }
            },
            async readFile(filepath) {
                const data = await this.apiCall(await this.getPath(filepath));
                return this.decodeBase64(data.content);
            },
            async saveFile(filepath, content, commitMessage = "Update via App") {
                const path = await this.getPath(filepath);
                let sha = null;
                try { sha = (await this.apiCall(path)).sha; } catch (e) {}
                await this.apiCall(path, 'PUT', { message: commitMessage, content: this.encodeBase64(content), sha });
            },
            async deleteFile(filepath, commitMessage = "Delete via App") {
                const path = await this.getPath(filepath);
                const existing = await this.apiCall(path);
                await this.apiCall(path, 'DELETE', { message: commitMessage, sha: existing.sha });
            }
        }
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
            const handle = await window.showDirectoryPicker();
            await App.Store.set('local_folder_handle', handle);
            document.getElementById('set-local-status').textContent = `Current: ${handle.name}`;
            App.Storage.Local.handle = handle;
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
    els.root.innerHTML = '<div style="padding: 24px;">Loading...</div>';

    try {
        if (!loadedModules[hash]) {
            loadedModules[hash] = await import(`./modules/${hash}.js`);
        }
        const module = loadedModules[hash];
        
        const moduleTitle = module.meta?.title || modLabel(hash);
        els.moduleTitle.textContent = moduleTitle;
        els.footerModule.textContent = moduleTitle;
        
        els.root.innerHTML = '';
        if (module.render) await module.render(els.root, App);
        
    } catch (error) {
        console.error(`Failed to load module: ${hash}`, error);
        els.root.innerHTML = `<div style="padding: 24px; color: var(--red);">Module '${hash}' not found yet. Create it in the modules/ folder.</div>`;
    }
}

initApp();