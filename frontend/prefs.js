// ============================================================
//  Nova AI Assistant — prefs.js
//  Per-user AI / UX preference store.
//
//  Load order: config.js → auth.js → prefs.js → script.js
//
//  Preferences are stored in localStorage under a key that includes
//  the user's ID, so each account on the same browser has independent
//  settings.  Falls back to a guest key when no session exists.
//
//  Shape (all fields optional / have defaults):
//  {
//    language:       'hinglish' | 'hindi' | 'english'   default: 'hinglish'
//    voice_enabled:  boolean                             default: true
//    auto_speak:     boolean                             default: true
//    response_style: 'short' | 'normal' | 'detailed'    default: 'normal'
//  }
//
//  Public API:
//    getPrefs()                     → prefs object (always returns defaults if missing)
//    savePrefs(partial)             → void  (merges into stored prefs)
//    getPref(key)                   → value
//    setPref(key, value)            → void
// ============================================================

'use strict';

const PREFS_NS = 'nova_prefs_';  // + userId or 'guest'

const PREFS_DEFAULTS = {
  language:       'hinglish',
  voice_enabled:  true,
  auto_speak:     true,
  response_style: 'normal',
};

function _prefsKey() {
  try {
    const session = (typeof getSession === 'function') ? getSession() : null;
    const uid = session?.user?.id;
    return PREFS_NS + (uid ? uid.slice(0, 16) : 'guest');
  } catch {
    return PREFS_NS + 'guest';
  }
}

function getPrefs() {
  try {
    const raw = localStorage.getItem(_prefsKey());
    const stored = raw ? JSON.parse(raw) : {};
    return { ...PREFS_DEFAULTS, ...stored };
  } catch {
    return { ...PREFS_DEFAULTS };
  }
}

function savePrefs(partial) {
  try {
    const current = getPrefs();
    const merged  = { ...current, ...partial };
    localStorage.setItem(_prefsKey(), JSON.stringify(merged));
  } catch (e) {
    console.warn('[prefs] savePrefs failed:', e);
  }
}

function getPref(key) {
  return getPrefs()[key] ?? PREFS_DEFAULTS[key];
}

function setPref(key, value) {
  savePrefs({ [key]: value });
}
