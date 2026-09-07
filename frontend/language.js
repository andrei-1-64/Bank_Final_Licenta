/* Lightweight, dependency-free page localisation. Bundles live in i18n/. */
(function () {
    'use strict';
    const STORAGE_KEY = 'bank_preferred_language';
    // Bump this whenever an i18n/*.json bundle's content changes. It drives both
    // the localStorage cache key below and the fetch cache-buster, so it's the
    // single place to touch - previously the two were hardcoded separately and
    // could drift out of sync, leaving browsers stuck on a stale bundle that
    // rendered raw "section.key" strings for any key added after their cache
    // was written.
    const I18N_VERSION = 26;
    const CACHE_PREFIX = `bank_i18n_v${I18N_VERSION}:`;
    const DEFAULT_LANGUAGE = 'ro';
    const LANGUAGES = { ro: 'Română', en: 'English', uk: 'Українська', hu: 'Magyar', tr: 'Türkçe', it: 'Italiano', es: 'Español', fr: 'Français', de: 'Deutsch' };
    // Every language's name, translated into every OTHER language - so the
    // selector menu itself reads in whichever language is currently active
    // (e.g. a Turkish UI lists "İngilizce", not "English") instead of always
    // showing each option in its own endonym. Keyed [displayLanguage][code].
    const LANGUAGE_NAMES = {
        ro: { ro: 'Română', en: 'Engleză', uk: 'Ucraineană', hu: 'Maghiară', tr: 'Turcă', it: 'Italiană', es: 'Spaniolă', fr: 'Franceză', de: 'Germană' },
        en: { ro: 'Romanian', en: 'English', uk: 'Ukrainian', hu: 'Hungarian', tr: 'Turkish', it: 'Italian', es: 'Spanish', fr: 'French', de: 'German' },
        uk: { ro: 'Румунська', en: 'Англійська', uk: 'Українська', hu: 'Угорська', tr: 'Турецька', it: 'Італійська', es: 'Іспанська', fr: 'Французька', de: 'Німецька' },
        hu: { ro: 'Román', en: 'Angol', uk: 'Ukrán', hu: 'Magyar', tr: 'Török', it: 'Olasz', es: 'Spanyol', fr: 'Francia', de: 'Német' },
        tr: { ro: 'Rumence', en: 'İngilizce', uk: 'Ukraynaca', hu: 'Macarca', tr: 'Türkçe', it: 'İtalyanca', es: 'İspanyolca', fr: 'Fransızca', de: 'Almanca' },
        it: { ro: 'Rumeno', en: 'Inglese', uk: 'Ucraino', hu: 'Ungherese', tr: 'Turco', it: 'Italiano', es: 'Spagnolo', fr: 'Francese', de: 'Tedesco' },
        es: { ro: 'Rumano', en: 'Inglés', uk: 'Ucraniano', hu: 'Húngaro', tr: 'Turco', it: 'Italiano', es: 'Español', fr: 'Francés', de: 'Alemán' },
        fr: { ro: 'Roumain', en: 'Anglais', uk: 'Ukrainien', hu: 'Hongrois', tr: 'Turc', it: 'Italien', es: 'Espagnol', fr: 'Français', de: 'Allemand' },
        de: { ro: 'Rumänisch', en: 'Englisch', uk: 'Ukrainisch', hu: 'Ungarisch', tr: 'Türkisch', it: 'Italienisch', es: 'Spanisch', fr: 'Französisch', de: 'Deutsch' },
    };
    function languageNameIn(displayLanguage, code) {
        return LANGUAGE_NAMES[displayLanguage]?.[code] || LANGUAGES[code];
    }
    const bundles = new Map();
    // Every translated text node's ORIGINAL (untranslated) string, keyed by
    // the DOM node itself. translateTextNode always re-translates FROM this,
    // never from the node's current (possibly already-translated) text -
    // without it, switching language A -> B -> A would translate B's output
    // as if it were source text, corrupting the page instead of restoring it.
    const sourceText = new WeakMap();
    let activeBundle = {};

    function setPath(target, path, value) {
        const parts = path.split('.');
        let cursor = target;
        parts.forEach((part, index) => {
            if (index === parts.length - 1) {
                cursor[part] = value;
                return;
            }
            if (!cursor[part] || typeof cursor[part] !== 'object') cursor[part] = {};
            cursor = cursor[part];
        });
    }

    function normalizeBundle(bundle) {
        const normalized = {};
        Object.entries(bundle || {}).forEach(([key, value]) => {
            setPath(normalized, key, value);
        });
        return normalized;
    }

    function mergeObjects(target, source) {
        Object.entries(source || {}).forEach(([key, value]) => {
            if (value && typeof value === 'object' && !Array.isArray(value)) {
                target[key] = mergeObjects(
                    target[key] && typeof target[key] === 'object' ? target[key] : {},
                    value,
                );
            } else {
                target[key] = value;
            }
        });
        return target;
    }

    function mergeBundles(...parts) {
        return parts.reduce((merged, part) => mergeObjects(merged, normalizeBundle(part)), {});
    }

    function preferredLanguage() {
        let saved = null;
        try { saved = localStorage.getItem(STORAGE_KEY); } catch { /* Storage is optional. */ }
        if (Object.prototype.hasOwnProperty.call(LANGUAGES, saved)) return saved;
        const browserLanguages = navigator.languages?.length ? navigator.languages : [navigator.language];
        return browserLanguages.map((value) => value?.split('-')[0].toLowerCase())
            .find((value) => Object.prototype.hasOwnProperty.call(LANGUAGES, value)) || DEFAULT_LANGUAGE;
    }
    async function loadBundle(language) {
        if (bundles.has(language)) return bundles.get(language);
        const cacheKey = `${CACHE_PREFIX}${language}`;
        try {
            const cached = localStorage.getItem(cacheKey);
            if (cached) { const bundle = normalizeBundle(JSON.parse(cached)); bundles.set(language, bundle); return bundle; }
        } catch { /* Replace an invalid cache with the shipped bundle. */ }
        const response = await fetch(`i18n/${language}.json?v=${I18N_VERSION}`, { cache: 'force-cache' });
        if (!response.ok) throw new Error(`Could not load language: ${language}`);
        const bundle = await response.json();
        const normalizedBundle = mergeBundles(bundle);
        bundles.set(language, normalizedBundle);
        try { localStorage.setItem(cacheKey, JSON.stringify(normalizedBundle)); } catch { /* Storage is optional. */ }
        return normalizedBundle;
    }
    function interpolate(value, params = {}) {
        return String(value).replace(/\{(\w+)\}/g, (_, name) => String(params[name] ?? `{${name}}`));
    }
    // Three-tier lookup, in order: (1) `value` as a dotted "section.key" path
    // into the bundle - the normal case; (2) `value` as a literal bare key
    // inside the "common" section - lets markup/JS pass a short key without
    // knowing/repeating its section; (3) that same bare key searched across
    // EVERY section, as a last resort for a key whose section the caller got
    // wrong or omitted. Falls through to returning `value` itself unchanged
    // (never throws) so a missing translation shows the key, not a crash.
    function translate(value, params = {}) {
        if (!value) return value;
        const result = value.split('.').reduce((current, part) => current?.[part], activeBundle);
        if (typeof result === 'string') return interpolate(result, params);
        if (typeof activeBundle.common?.[value] === 'string') return interpolate(activeBundle.common[value], params);
        for (const section of Object.values(activeBundle)) {
            if (section && typeof section === 'object' && typeof section[value] === 'string') return interpolate(section[value], params);
        }
        return value;
    }
    function translateTextNode(node) {
        if (!node.nodeValue.trim()) return;
        const parent = node.parentElement;
        // .notranslate marks browser/OS "offer to translate this page"
        // hints off-limits to OUR translator too (e.g. proper nouns) - except
        // inside .language-selector, which is itself marked .notranslate
        // (so a browser translate feature never mangles it) but still needs
        // ITS OWN option labels (the language names) run through our i18n.
        if (!parent || parent.closest('script, style, [data-i18n-ignore]') || (parent.closest('.notranslate') && !parent.closest('.language-selector'))) return;
        const original = sourceText.get(node) || node.nodeValue;
        sourceText.set(node, original);
        const leading = original.match(/^\s*/)[0], trailing = original.match(/\s*$/)[0];
        node.nodeValue = `${leading}${translate(original.trim())}${trailing}`;
    }
    function translateElement(element) {
        if (element.matches?.('[data-i18n-ignore]') || element.closest?.('[data-i18n-ignore]') || (element.matches?.('.notranslate, .notranslate *') && !element.closest?.('.language-selector'))) return;
        if (element.hasAttribute?.('data-i18n')) {
            const key = element.getAttribute('data-i18n');
            let params = {};
            try { params = element.dataset.i18nParams ? JSON.parse(element.dataset.i18nParams) : {}; } catch { /* Ignore malformed optional metadata. */ }
            element.textContent = translate(key, params);
        }
        // Same anti-double-translation guard as `sourceText` above, but for
        // attributes instead of text nodes (a WeakMap can't key off an
        // attribute, so the original is stashed in a sibling DOM attribute
        // instead): `data-i18n-source-${attribute}` is written once, on
        // first translation, and every later re-translation (language
        // switch) reads FROM that stashed original - never from the
        // attribute's current, possibly-already-translated value.
        ['placeholder', 'title', 'aria-label'].forEach((attribute) => {
            const source = `data-i18n-source-${attribute}`;
            const keyAttribute = `data-i18n-${attribute}`;
            if (!element.hasAttribute?.(attribute) && !element.hasAttribute?.(keyAttribute)) return;
            const key = element.getAttribute(keyAttribute);
            const original = element.getAttribute(source) || element.getAttribute(attribute);
            if (original) element.setAttribute(source, original);
            element.setAttribute(attribute, key ? translate(key) : translate(original));
        });
        element.childNodes.forEach((node) => { if (node.nodeType === Node.TEXT_NODE) translateTextNode(node); });
    }
    function translatePage(root = document.body) {
        if (!root) return;
        if (root.nodeType === Node.ELEMENT_NODE) translateElement(root);
        root.querySelectorAll?.('*').forEach(translateElement);
        const titleSource = document.documentElement.dataset.i18nTitle || document.title;
        document.documentElement.dataset.i18nTitle = titleSource;
        document.title = translate(titleSource);
        document.querySelectorAll('[data-i18n-content]').forEach((element) => {
            element.setAttribute('content', translate(element.dataset.i18nContent));
        });
    }
    async function activateLanguage(language) {
        activeBundle = await loadBundle(language);
        document.documentElement.lang = language;
        translatePage();
        document.documentElement.classList.remove('i18n-pending');
        window.dispatchEvent(new CustomEvent('languagechange', { detail: { language } }));
    }
    function addSelector() {
        const selector = document.createElement('div');
        selector.className = 'language-selector notranslate';
        selector.setAttribute('translate', 'no');
        selector.innerHTML = `<button type="button" class="language-selector-trigger" data-i18n-aria-label="common.Alege limba" aria-haspopup="listbox" aria-expanded="false"><span class="language-selector-icon" aria-hidden="true">◎</span><span class="language-selector-current"></span><span class="language-selector-chevron" aria-hidden="true"></span></button><div class="language-selector-menu" role="listbox" data-i18n-aria-label="common.Alege limba" hidden>${Object.entries(LANGUAGES).map(([code, name]) => `<button type="button" class="language-selector-option" role="option" data-language="${code}">${name}</button>`).join('')}</div>`;
        const trigger = selector.querySelector('.language-selector-trigger');
        const menu = selector.querySelector('.language-selector-menu');
        const current = selector.querySelector('.language-selector-current');
        const setSelected = (language) => {
            current.textContent = LANGUAGES[language];
            selector.querySelectorAll('.language-selector-option').forEach((option) => {
                option.textContent = languageNameIn(language, option.dataset.language);
                option.setAttribute('aria-selected', String(option.dataset.language === language));
            });
        };
        const close = () => { menu.hidden = true; trigger.setAttribute('aria-expanded', 'false'); };
        setSelected(preferredLanguage());
        trigger.addEventListener('click', () => { menu.hidden = !menu.hidden; trigger.setAttribute('aria-expanded', String(!menu.hidden)); });
        selector.querySelectorAll('.language-selector-option').forEach((option) => option.addEventListener('click', async () => {
            const language = option.dataset.language;
            setSelected(language); close(); localStorage.setItem(STORAGE_KEY, language);
            await activateLanguage(language);
        }));
        window.addEventListener('languagechange', () => { translateElement(trigger); translateElement(menu); setSelected(preferredLanguage()); });
        document.addEventListener('click', (event) => { if (!selector.contains(event.target)) close(); });
        document.addEventListener('keydown', (event) => { if (event.key === 'Escape') close(); });
        const actions = document.querySelector('.top-header .user-actions');
        const logout = actions?.querySelector('.logout-btn');
        if (logout) actions.insertBefore(selector, logout); else document.body.appendChild(selector);
    }
    window.t = (key, fallback = key, params = {}) => translate(key, params) === key
        ? interpolate(fallback, params)
        : translate(key, params);
    window.refreshTranslations = () => translatePage();
    document.addEventListener('DOMContentLoaded', async () => {
        addSelector();
        // app.js/admin.js render most of the UI (proposal cards, chat
        // history, notification lists, ...) AFTER this file's initial pass,
        // via plain DOM APIs that know nothing about i18n - this observer is
        // what actually translates that later content, by re-running the
        // same translate functions on every node it sees appended anywhere
        // in the page.
        new MutationObserver((mutations) => mutations.forEach((mutation) => mutation.addedNodes.forEach((node) => {
            if (node.nodeType === Node.TEXT_NODE) translateTextNode(node);
            else if (node.nodeType === Node.ELEMENT_NODE) translatePage(node);
        }))).observe(document.body, { childList: true, subtree: true });
        const language = preferredLanguage();
        try { await activateLanguage(language); } catch { document.documentElement.classList.remove('i18n-pending'); }
        // Warm the cache for every OTHER language in the background (fire
        // and forget, errors ignored) so switching languages later is
        // instant instead of waiting on a fetch - never blocks the page's
        // own load on languages nobody may ever select.
        Object.keys(LANGUAGES).filter((code) => code !== language).forEach((code) => loadBundle(code).catch(() => {}));
    });
})();
