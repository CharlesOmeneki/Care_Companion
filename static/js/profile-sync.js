// static/js/profile-sync.js
// Minimal, safe shim to make localStorage per-signed-in-user.
// - Prefers `_user_<id>:<key>` keys but FALLS BACK to legacy unprefixed keys on read.
// - Writes ONLY the prefixed per-user keys (prevents cross-user leakage).
// - Exposes window.__userLS JSON helper (get/set/remove/prefixedKey,userId).
// - Re-emits storage events with unprefixed keys so existing listeners keep working.
//
// Usage:
// 1) Render window.__USER_ID__ (or <body data-user-id="...">) before loading this script.
// 2) Include this script BEFORE any inline script that reads/writes localStorage.
//
// Note: This only patches localStorage.getItem/setItem/removeItem (non-invasive).
//       It intentionally does NOT override Storage.prototype.key() or .length.

(function () {
  'use strict';

  // --- derive user id (prefer explicit window.__USER_ID__, then body data attribute) ---
  var uid = null;
  try {
    if (typeof window !== 'undefined' && typeof window.__USER_ID__ !== 'undefined' && window.__USER_ID__ !== null) {
      uid = String(window.__USER_ID__);
    }
  } catch (e) { /* ignore */ }

  try {
    if (!uid && typeof document !== 'undefined' && document && document.body && document.body.dataset && document.body.dataset.userId) {
      uid = String(document.body.dataset.userId);
    }
  } catch (e) { /* ignore */ }

  if (!uid) uid = '_anon';

  // --- page-level apply guard flag (consumers may set this before loading script) ---
  try {
    var applyFlag = true;
    if (typeof window !== 'undefined' && typeof window.__PROFILE_SYNC_APPLY_TO_EDIT__ !== 'undefined') {
      if (window.__PROFILE_SYNC_APPLY_TO_EDIT__ === false) applyFlag = false;
    }
    // surface read-only-ish flag for other scripts
    try { Object.defineProperty(window, '__PROFILE_SYNC_SHOULD_APPLY__', { value: applyFlag, configurable: true }); } catch (e) { window.__PROFILE_SYNC_SHOULD_APPLY__ = applyFlag; }
  } catch (e) {
    try { window.__PROFILE_SYNC_SHOULD_APPLY__ = true; } catch (_) { /* ignore */ }
  }

  // --- helpers ---
  function userKey(key) {
    try {
      if (typeof key !== 'string') return key;
      // Already prefixed?
      if (key.indexOf('_user_') === 0) return key;
      return '_user_' + uid + ':' + key;
    } catch (e) {
      return key;
    }
  }

  // Save originals
  var storage = window.localStorage;
  var origGet = storage.getItem.bind(storage);
  var origSet = storage.setItem.bind(storage);
  var origRemove = storage.removeItem.bind(storage);

  // --- tolerant get/set/remove that works with both prefixed and legacy keys ---
  try {
    // getItem: prefer prefixed, fallback to unprefixed
    storage.getItem = function (k) {
      try {
        // If caller passed a prefixed key already, return it directly
        if (typeof k === 'string' && k.indexOf('_user_') === 0) {
          return origGet(k);
        }
        var prefixedVal = null;
        try { prefixedVal = origGet(userKey(k)); } catch (_) { prefixedVal = null; }
        if (prefixedVal !== null && typeof prefixedVal !== 'undefined') {
          return prefixedVal;
        }
        // fallback to legacy unprefixed key
        return origGet(k);
      } catch (e) {
        // final fallback
        try { return origGet(k); } catch (err) { return null; }
      }
    };

    // setItem: write only the prefixed per-user key (do NOT overwrite legacy unprefixed keys).
    // Also dispatch a synthetic storage event with the unprefixed key so same-tab listeners
    // that expect unprefixed keys still get notified (and other tabs will receive the real
    // storage event for the prefixed key which our listener will re-emit to an unprefixed key).
    storage.setItem = function (k, v) {
      try {
        // if caller passed a prefixed key intentionally, still write prefixed
        var target = (typeof k === 'string' && k.indexOf('_user_') === 0) ? k : userKey(k);
        origSet(target, v);

        // Try to notify same-tab listeners with an event that uses the unprefixed key.
        // Many browsers won't let you construct StorageEvent in all contexts; we fallback to CustomEvent.
        try {
          var unpref = (typeof k === 'string' && k.indexOf('_user_') === 0) ? k.split(':').slice(1).join(':') : String(k);
          try {
            // StorageEvent constructor (may fail in some browsers)
            var evt = new StorageEvent('storage', {
              key: unpref,
              oldValue: null,
              newValue: v,
              url: (document && document.URL) || '',
              storageArea: window.localStorage
            });
            window.dispatchEvent(evt);
          } catch (err) {
            // fallback: CustomEvent with detail payload
            try {
              var ce = new CustomEvent('__user_storage__', { detail: { key: unpref, newValue: v } });
              window.dispatchEvent(ce);
            } catch (err2) {
              // as very last resort, write a short-lived marker (not ideal but safe)
              try {
                origSet(userKey('__last_write_marker__'), Date.now().toString());
                setTimeout(function () { try { origRemove(userKey('__last_write_marker__')); } catch (_) { } }, 500);
              } catch (_) { /* ignore */ }
            }
          }
        } catch (notifyErr) { /* ignore notify errors */ }

        return;
      } catch (e) {
        // fallback: write unprefixed (very last resort)
        try { return origSet(k, v); } catch (err2) { /* ignore */ }
      }
    };

    // removeItem: attempt to remove both prefixed and unprefixed keys to clean up,
    // but writing behavior never creates unprefixed keys anymore.
    storage.removeItem = function (k) {
      try {
        try { origRemove(userKey(k)); } catch (err) { /* ignore */ }
        try { origRemove(k); } catch (err) { /* ignore */ }
        // also broadcast removal to same-tab listeners (unprefixed key)
        try {
          var unpref = (typeof k === 'string' && k.indexOf('_user_') === 0) ? k.split(':').slice(1).join(':') : String(k);
          try {
            var evt = new StorageEvent('storage', {
              key: unpref,
              oldValue: null,
              newValue: null,
              url: (document && document.URL) || '',
              storageArea: window.localStorage
            });
            window.dispatchEvent(evt);
          } catch (err) {
            try {
              window.dispatchEvent(new CustomEvent('__user_storage__', { detail: { key: unpref, newValue: null } }));
            } catch (err2) { /* ignore */ }
          }
        } catch (_) { /* ignore */ }
        return;
      } catch (e) {
        try { return origRemove(k); } catch (err2) { /* ignore */ }
      }
    };
  } catch (e) {
    // If the environment forbids monkey-patching localStorage, warn and continue
    try { console.warn('profile-sync: could not patch localStorage methods', e); } catch (_) { /* ignore */ }
  }

  // --- JSON-friendly explicit helper API (useful for future code) ---
  // __userLS uses the original bound methods to operate on the prefixed key directly.
  window.__userLS = {
    get: function (key, fallback) {
      if (typeof fallback === 'undefined') fallback = null;
      try {
        var raw = null;
        try { raw = origGet(userKey(key)); } catch (e) { raw = null; }
        if (raw === null || typeof raw === 'undefined') {
          // fallback to legacy key (read-only fallback)
          raw = origGet(key);
          if (raw === null || typeof raw === 'undefined') return fallback;
        }
        try { return JSON.parse(raw); } catch (e) { return raw; }
      } catch (e) {
        return fallback;
      }
    },
    set: function (key, value) {
      try {
        var out = (typeof value === 'string') ? value : JSON.stringify(value);
        // write only the prefixed key (do NOT write legacy unprefixed keys)
        origSet(userKey(key), out);
        // also attempt to notify same-tab listeners (unprefixed)
        try {
          var unpref = String(key);
          try {
            var evt = new StorageEvent('storage', {
              key: unpref,
              oldValue: null,
              newValue: out,
              url: (document && document.URL) || '',
              storageArea: window.localStorage
            });
            window.dispatchEvent(evt);
          } catch (err) {
            try { window.dispatchEvent(new CustomEvent('__user_storage__', { detail: { key: unpref, newValue: out } })); } catch (_) { /* ignore */ }
          }
        } catch (_) { /* ignore */ }
      } catch (e) { /* ignore */ }
    },
    remove: function (key) {
      try { origRemove(userKey(key)); } catch (e) { /* ignore */ }
      try { origRemove(key); } catch (e) { /* ignore */ }
      try {
        var unpref = String(key);
        try {
          var evt = new StorageEvent('storage', {
            key: unpref,
            oldValue: null,
            newValue: null,
            url: (document && document.URL) || '',
            storageArea: window.localStorage
          });
          window.dispatchEvent(evt);
        } catch (err) {
          try { window.dispatchEvent(new CustomEvent('__user_storage__', { detail: { key: unpref, newValue: null } })); } catch (_) { /* ignore */ }
        }
      } catch (_) { /* ignore */ }
    },
    // convenience helpers
    prefixedKey: function (key) { return userKey(key); },
    userId: uid
  };

  // --- re-emit storage events with unprefixed keys for backward compatibility ---
  // When other tabs write the prefixed key, many existing listeners may still be checking
  // for unprefixed keys. This block re-dispatches a synthetic StorageEvent (or fallback
  // CustomEvent) with the unprefixed key so existing code continues to work.
  try {
    window.addEventListener('storage', function (e) {
      try {
        if (!e || !e.key) return;
        // compute prefix string used by userKey('')
        var pref = userKey(''); // e.g. "_user_<id>:"
        if (typeof e.key === 'string' && e.key.indexOf(pref) === 0) {
          var un = e.key.slice(pref.length);
          try {
            var evt = new StorageEvent('storage', {
              key: un,
              oldValue: e.oldValue,
              newValue: e.newValue,
              url: e.url || (document && document.URL),
              storageArea: window.localStorage
            });
            window.dispatchEvent(evt);
          } catch (err) {
            // Some browsers restrict constructing StorageEvent directly — fallback:
            try {
              var fallback = new CustomEvent('__user_storage__', { detail: { key: un, oldValue: e.oldValue, newValue: e.newValue } });
              window.dispatchEvent(fallback);
            } catch (err2) { /* ignore */ }
          }
        }
      } catch (err) { /* ignore */ }
    }, false);
  } catch (e) { /* ignore */ }

  // Developer hint (no-op): inspect window.__userLS.userId for debugging if needed.
})();
