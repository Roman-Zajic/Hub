export const meta = {
    title: 'Work Notes (Local)'
};

export async function render(root, App) {
    root.innerHTML = `
        <div class="notes-layout">
            <div class="file-panel">
                <div class="panel-actions">
                    <button id="wn-new-btn" class="btn btn-outline" style="width: 100%;">+ New Note</button>
                </div>
                <div id="wn-file-list" class="file-list"></div>
            </div>
            
            <div class="editor-panel">
                <div class="editor-toolbar">
                    <input type="text" id="wn-title" class="input" style="flex:1" placeholder="Filename (e.g., meeting.md)">
                    <span id="wn-status" style="font-size: 0.75rem; color: var(--teal-400); font-weight: 600; margin-right: 8px;"></span>
                    <button id="wn-toggle-btn" class="btn btn-outline btn-sm">Preview</button>
                    <button id="wn-save-btn" class="btn btn-primary btn-sm">Save</button>
                    <button id="wn-delete-btn" class="btn btn-danger btn-sm" style="display:none;">Delete</button>
                </div>
                <textarea id="wn-editor" class="editor-textarea" placeholder="# Start writing..."></textarea>
                <div id="wn-preview" class="preview-area"></div>
            </div>
        </div>
    `;

    if (!window.marked) {
        await new Promise((resolve) => {
            const script = document.createElement('script');
            script.src = 'https://cdn.jsdelivr.net/npm/marked/marked.min.js';
            script.onload = resolve;
            document.head.appendChild(script);
        });
    }

    const els = {
        fileList: document.getElementById('wn-file-list'),
        title: document.getElementById('wn-title'),
        editor: document.getElementById('wn-editor'),
        preview: document.getElementById('wn-preview'),
        btnNew: document.getElementById('wn-new-btn'),
        btnSave: document.getElementById('wn-save-btn'),
        btnDelete: document.getElementById('wn-delete-btn'),
        btnToggle: document.getElementById('wn-toggle-btn'),
        status: document.getElementById('wn-status')
    };

    let currentFile = null;
    let isPreview = false;
    let saveTimeout = null;

    async function loadFileList() {
        try {
            const files = await App.Storage.Local.listFiles();
            els.fileList.innerHTML = '';
            
            files.sort().forEach(filename => {
                const div = document.createElement('div');
                div.className = `file-item ${filename === currentFile ? 'active' : ''}`;
                div.textContent = filename.replace('.md', '');
                div.onclick = () => openFile(filename);
                els.fileList.appendChild(div);
            });
        } catch (e) {
            els.fileList.innerHTML = `<div style="padding: 10px; font-size: 0.8rem; color: var(--red);">Please select a local folder in Settings first.</div>`;
        }
    }

    async function openFile(filename) {
        try {
            const content = await App.Storage.Local.readFile(filename);
            currentFile = filename;
            els.title.value = filename.replace('.md', '');
            els.editor.value = content;
            els.btnDelete.style.display = 'block';
            els.status.textContent = '';
            loadFileList();
            if (isPreview) updatePreview();
        } catch (e) {
            alert("Error reading file: " + e.message);
        }
    }

    async function saveNote() {
        if (!currentFile && !els.title.value.trim()) {
            alert("Please enter a title.");
            return;
        }

        els.status.textContent = 'Saving...';
        
        let filename = els.title.value.trim();
        if (!filename.endsWith('.md')) filename += '.md';

        try {
            if (currentFile && currentFile !== filename) {
                await App.Storage.Local.deleteFile(currentFile);
            }

            await App.Storage.Local.saveFile(filename, els.editor.value);
            currentFile = filename;
            els.btnDelete.style.display = 'block';
            els.status.textContent = 'Saved';
            
            setTimeout(() => els.status.textContent = '', 2000);
            loadFileList();
        } catch (e) {
            els.status.textContent = 'Error saving';
            console.error(e);
        }
    }

    async function deleteNote() {
        if (!currentFile) return;
        if (!confirm(`Delete "${currentFile}"?`)) return;
        
        try {
            await App.Storage.Local.deleteFile(currentFile);
            currentFile = null;
            els.title.value = '';
            els.editor.value = '';
            els.btnDelete.style.display = 'none';
            loadFileList();
        } catch (e) {
            alert("Error deleting file.");
        }
    }

    function updatePreview() {
        const text = els.editor.value;
        const processed = text.replace(/\[\[([^\]]+)\]\]/g, (match, noteRef) => {
            let filename = noteRef.trim();
            if (!filename.endsWith('.md')) filename += '.md';
            return `[${noteRef.trim()}](#note-${encodeURIComponent(filename)})`;
        });
        els.preview.innerHTML = window.marked.parse(processed);
    }

    els.preview.addEventListener('click', (e) => {
        const a = e.target.closest('a');
        if (a && a.getAttribute('href') && a.getAttribute('href').startsWith('#note-')) {
            e.preventDefault();
            const filename = decodeURIComponent(a.getAttribute('href').replace('#note-', ''));
            openFile(filename);
        }
    });

    function toggleMode() {
        isPreview = !isPreview;
        if (isPreview) {
            updatePreview();
            els.editor.style.display = 'none';
            els.preview.style.display = 'block';
            els.btnToggle.textContent = 'Edit';
        } else {
            els.editor.style.display = 'block';
            els.preview.style.display = 'none';
            els.btnToggle.textContent = 'Preview';
        }
    }

    els.btnNew.onclick = () => {
        currentFile = null;
        els.title.value = '';
        els.editor.value = '';
        els.btnDelete.style.display = 'none';
        els.status.textContent = '';
        if (isPreview) toggleMode();
        loadFileList();
    };

    els.btnSave.onclick = saveNote;
    els.btnDelete.onclick = deleteNote;
    els.btnToggle.onclick = toggleMode;

    els.editor.addEventListener('input', () => {
        els.status.textContent = 'Unsaved changes...';
        clearTimeout(saveTimeout);
        saveTimeout = setTimeout(saveNote, 1000);
    });

    loadFileList();
}