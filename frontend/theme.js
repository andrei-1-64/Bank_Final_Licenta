/* Dark/light theme switcher. Dependency-free and self-injecting, same
 * pattern as language.js's language selector: the toggle button isn't part
 * of any page's markup, this script builds it and drops it into
 * .top-header .user-actions (dashboard/admin) or, on the header-less auth
 * pages, straight onto <body> (styled fixed top-left by style.css).
 *
 * The actual "apply the saved theme" step happens twice on purpose. The
 * inline snippet each HTML <head> carries runs first, before first paint, so
 * returning visitors never see a flash of the wrong theme. This file's own
 * call right below re-applies it as a harmless no-op safeguard for the rare
 * case that snippet is missing from a page - and is also this module's
 * single source of truth for the storage key and the two theme names.
 */
(function () {
    'use strict';
    const STORAGE_KEY = 'bank_theme';
    const root = document.documentElement;

    function storedTheme() {
        try { return localStorage.getItem(STORAGE_KEY); } catch { return null; }
    }

    function applyTheme(theme) {
        if (theme === 'light') root.setAttribute('data-theme', 'light');
        else root.removeAttribute('data-theme');
    }

    applyTheme(storedTheme());

    const SUN_ICON = '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4"></circle><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41"></path></svg>';
    const MOON_ICON = '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3a7 7 0 0 0 9.79 9.79z"></path></svg>';

    function addToggle() {
        if (document.querySelector('.theme-toggle-btn')) return;

        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'theme-toggle-btn';
        button.setAttribute('aria-label', 'Comută tema');
        button.setAttribute('data-i18n-aria-label', 'common.Comută tema');

        const paint = () => {
            const isLight = root.getAttribute('data-theme') === 'light';
            // Icon shows the theme a click will switch to, matching the
            // sun/moon convention used elsewhere for this kind of toggle.
            button.innerHTML = isLight ? MOON_ICON : SUN_ICON;
            button.setAttribute('aria-pressed', String(isLight));
        };
        paint();

        button.addEventListener('click', () => {
            const next = root.getAttribute('data-theme') === 'light' ? 'dark' : 'light';
            applyTheme(next);
            try { localStorage.setItem(STORAGE_KEY, next); } catch { /* Storage is optional. */ }
            paint();
        });

        const actions = document.querySelector('.top-header .user-actions');
        if (actions) {
            const logout = actions.querySelector('.logout-btn');
            actions.insertBefore(button, logout || null);
        } else {
            document.body.appendChild(button);
        }
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', addToggle);
    } else {
        addToggle();
    }
})();
