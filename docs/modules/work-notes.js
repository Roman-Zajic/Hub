// Work Notes = the Notes engine (modules/notes.js) running on the local folder <working directory>/Notes:
// same screen and features, no GitHub, no sync, works offline. All the logic lives in notes.js (mode 'local').
const V = new URL(import.meta.url).search;   // same cache-busting version as every other file (see app.js)
const { createNotes } = await import('./notes.js' + V);

export const { meta, render, unmount } = createNotes('local');
