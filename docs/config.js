export const APP_CONFIG = {
    // Every module that exists. Which of them are shown is chosen per device in Settings.
    modules: [
        'work-notes',
        'notes',
        'email-composer',
        'timers',
        'transcript',
		'comparer'
    ],
    // Loaded when the app opens (not only when you visit them): they keep working in the background.
    background: ['transcript'],
    // Libraries loaded on demand from a CDN (one place, so every module uses the same version)
    libs: {
        marked: 'https://cdn.jsdelivr.net/npm/marked@15/marked.min.js',
        d3: 'https://cdnjs.cloudflare.com/ajax/libs/d3/7.9.0/d3.min.js',
        mathjax: 'https://cdn.jsdelivr.net/npm/mathjax@4/tex-chtml.js'
    },
    brandName: 'Roman Zajic',
    defaultModule: 'work-notes'
};
