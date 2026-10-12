export const APP_CONFIG = {
    // Every module that exists. Which of them are shown is chosen per device in Settings.
    modules: [
        'work-notes',
        'notes',
        'email-composer',
        'timers',
        'transcript'
    ],
    // Loaded when the app opens (not only when you visit them): they keep working in the background.
    background: ['transcript'],
    brandName: 'Roman Zajic',
    defaultModule: 'work-notes'
};
