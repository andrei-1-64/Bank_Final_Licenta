/**
 * Admin panel.
 *
 * The real gate is server-side (require_admin on every /admin route). What
 * this file does is cosmetic: it hides a page a non-admin would only see
 * errors on. Never treat the checks here as security - a 403 from the API is
 * the authority, and every render path assumes the API could refuse.
 */

const ADMIN_PAGE_SIZE = 50;

/** The signed-in admin's own id, from GET /admin/me. Used only to grey out
 *  the self-block / self-demote buttons - the server refuses both anyway. */
let currentAdminId = null;

/** The user whose detail panel is open, so the transaction filter knows who
 *  it is filtering for without re-reading the DOM. */
let openUserId = null;

/** Every value rendered below is user-supplied (names, addresses, audit
 *  entity strings), so it goes through here before touching innerHTML.
 *  Someone could register with a name containing markup, and the admin is
 *  exactly the person you would aim that at. */
function esc(value) {
    if (value === null || value === undefined) return '';
    return String(value)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

function formatDateTime(value) {
    if (!value) return '—';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return '—';
    return date.toLocaleString('ro-RO', {
        day: '2-digit', month: '2-digit', year: 'numeric',
        hour: '2-digit', minute: '2-digit',
    });
}

function showAdminError(message = '') {
    const el = document.getElementById('admin-error');
    el.textContent = message;
    el.hidden = !message;
}

function emptyRow(table, colspan, text) {
    table.querySelector('tbody').innerHTML =
        `<tr><td class="admin-empty" colspan="${colspan}">${esc(text)}</td></tr>`;
}

// ---------------------------------------------------------------------------
// Entry point: prove admin, or leave.
// ---------------------------------------------------------------------------

document.addEventListener('DOMContentLoaded', async () => {
    let identity;
    try {
        identity = await apiFetch('/admin/me');
    } catch (err) {
        // 401 = not logged in at all; 403 = logged in but not an admin.
        // Anything else (e.g. migration 0016 not applied yet) is worth
        // showing rather than silently bouncing, so it can be diagnosed.
        if (err.status === 401) {
            window.location.href = 'login.html';
            return;
        }
        if (err.status === 403) {
            window.location.href = 'index.html';
            return;
        }
        document.body.innerHTML =
            `<p style="padding:32px;color:#EF4444">Panoul de administrare nu a putut fi ` +
            `încărcat: ${esc(err.message)}</p>`;
        return;
    }

    currentAdminId = identity.id;
    document.getElementById('admin-identity').textContent = identity.email;
    document.getElementById('admin-shell').hidden = false;
    if (window.lucide) lucide.createIcons();

    wireNavigation();
    wireFilters();
    await loadStats();
});

function wireNavigation() {
    const titles = {
        stats: 'Statistici',
        users: 'Utilizatori',
        orders: 'Comenzi carduri',
        audit: 'Jurnal audit',
    };
    const loaders = {
        stats: loadStats,
        users: loadUsers,
        orders: loadCardOrders,
        audit: loadAuditLog,
    };

    document.querySelectorAll('.nav-item[data-view]').forEach((button) => {
        button.addEventListener('click', async () => {
            const view = button.dataset.view;

            document.querySelectorAll('.nav-item').forEach((b) => b.classList.remove('active'));
            button.classList.add('active');
            document.querySelectorAll('.view').forEach((v) => v.classList.remove('active'));
            document.getElementById(`view-${view}`).classList.add('active');
            document.getElementById('admin-view-title').textContent = titles[view];

            showAdminError();
            await loaders[view]();
        });
    });
}

function wireFilters() {
    // Debounced so typing doesn't fire a request per keystroke.
    let searchTimer = null;
    document.getElementById('admin-user-search').addEventListener('input', () => {
        clearTimeout(searchTimer);
        searchTimer = setTimeout(loadUsers, 350);
    });

    let auditTimer = null;
    document.getElementById('admin-audit-filter').addEventListener('input', () => {
        clearTimeout(auditTimer);
        auditTimer = setTimeout(loadAuditLog, 350);
    });

    document.getElementById('admin-order-status').addEventListener('change', loadCardOrders);
}

// ---------------------------------------------------------------------------
// Statistici
// ---------------------------------------------------------------------------

async function loadStats() {
    const grid = document.getElementById('admin-stat-grid');
    const table = document.getElementById('admin-currency-table');

    let stats;
    try {
        stats = await apiFetch('/admin/stats');
    } catch (err) {
        showAdminError(`Statisticile nu au putut fi încărcate: ${err.message}`);
        return;
    }

    const tiles = [
        ['Utilizatori', stats.total_users],
        ['Conturi', stats.total_accounts],
        ['Carduri', stats.total_cards],
        ['Comenzi în așteptare', stats.pending_card_orders],
    ];
    grid.innerHTML = tiles
        .map(([label, value]) => `
            <div class="admin-stat">
                <span class="admin-stat-label">${esc(label)}</span>
                <span class="admin-stat-value">${esc(value)}</span>
            </div>`)
        .join('');

    if (!stats.totals_by_currency.length) {
        emptyRow(table, 3, 'Niciun cont încă.');
        return;
    }
    table.querySelector('tbody').innerHTML = stats.totals_by_currency
        .map((row) => `
            <tr>
                <td>${esc(row.currency)}</td>
                <td>${esc(row.account_count)}</td>
                <td class="num">${esc(formatMoney(row.total_minor, row.currency))}</td>
            </tr>`)
        .join('');
}

// ---------------------------------------------------------------------------
// Utilizatori
// ---------------------------------------------------------------------------

async function loadUsers() {
    const table = document.getElementById('admin-users-table');
    const search = document.getElementById('admin-user-search').value.trim();
    const params = new URLSearchParams({ limit: ADMIN_PAGE_SIZE });
    if (search) params.set('search', search);

    let users;
    try {
        users = await apiFetch(`/admin/users?${params}`);
    } catch (err) {
        showAdminError(`Utilizatorii nu au putut fi încărcați: ${err.message}`);
        return;
    }

    if (!users.length) {
        emptyRow(table, 6, search ? 'Niciun rezultat.' : 'Niciun utilizator.');
        document.getElementById('admin-user-detail').hidden = true;
        return;
    }

    table.querySelector('tbody').innerHTML = users
        .map((u) => {
            const blocked = Boolean(u.blocked_at);
            // The acting admin can neither block nor demote themselves - the
            // server refuses both (see service.set_user_role /
            // set_user_blocked); disabling the buttons just avoids offering
            // an action that would only come back as a 403.
            const isSelf = u.id === currentAdminId;
            return `
            <tr data-user-id="${esc(u.id)}">
                <td class="row-clickable">${esc(u.first_name)} ${esc(u.last_name)}</td>
                <td>${esc(u.email)}</td>
                <td class="nowrap">${u.role === 'admin'
                    ? '<span class="admin-badge admin">admin</span>'
                    : esc(u.role)}</td>
                <td class="nowrap">${blocked
                    ? `<span class="admin-badge cancelled" title="Blocat la ${esc(formatDateTime(u.blocked_at))}">blocat</span>`
                    : '<span class="admin-badge delivered">activ</span>'}</td>
                <td class="nowrap">${esc(formatDateTime(u.created_at))}</td>
                <td class="nowrap">
                    <div class="admin-row-actions">
                        <button class="admin-mini-btn" data-action="block"
                                data-user-id="${esc(u.id)}" data-blocked="${blocked}"
                                ${isSelf ? 'disabled title="Nu te poți bloca singur"' : ''}>
                            ${blocked ? 'Deblochează' : 'Blochează'}
                        </button>
                        <button class="admin-mini-btn" data-action="role"
                                data-user-id="${esc(u.id)}" data-role="${esc(u.role)}"
                                ${isSelf ? 'disabled title="Nu îți poți schimba propriul rol"' : ''}>
                            ${u.role === 'admin' ? 'Fă utilizator' : 'Fă admin'}
                        </button>
                    </div>
                </td>
            </tr>`;
        })
        .join('');

    table.querySelectorAll('td.row-clickable').forEach((cell) => {
        cell.addEventListener('click', () =>
            loadUserDetail(cell.closest('tr').dataset.userId));
    });
    table.querySelectorAll('button[data-action="block"]').forEach((b) => {
        b.addEventListener('click', () => toggleBlocked(b));
    });
    table.querySelectorAll('button[data-action="role"]').forEach((b) => {
        b.addEventListener('click', () => toggleRole(b));
    });
}

/** Both actions are offered in two places - the users list and the open
 *  detail panel - so the API call lives here once and each caller decides
 *  what to refresh afterwards. */
async function applyBlockChange(userId, currentlyBlocked) {
    if (!currentlyBlocked &&
        !window.confirm('Blochezi acest utilizator? Sesiunile lui active vor fi închise imediat.')) {
        return false;
    }
    await apiFetch(`/admin/users/${encodeURIComponent(userId)}/blocked`, {
        method: 'PATCH',
        body: JSON.stringify({ blocked: !currentlyBlocked }),
    });
    return true;
}

async function applyRoleChange(userId, currentRole) {
    const nextRole = currentRole === 'admin' ? 'customer' : 'admin';
    if (nextRole === 'admin' &&
        !window.confirm('Îi dai acestui utilizator drepturi de administrator? ' +
                        'Va putea vedea datele tuturor clienților.')) {
        return false;
    }
    await apiFetch(`/admin/users/${encodeURIComponent(userId)}/role`, {
        method: 'PATCH',
        body: JSON.stringify({ role: nextRole }),
    });
    return true;
}

async function toggleBlocked(button) {
    const { userId } = button.dataset;
    button.disabled = true;
    try {
        const changed = await applyBlockChange(userId, button.dataset.blocked === 'true');
        if (!changed) { button.disabled = false; return; }
        await loadUsers();
        // Keep an open detail panel in step with the list.
        if (openUserId === userId) await loadUserDetail(userId);
    } catch (err) {
        showAdminError(`Starea contului nu a putut fi schimbată: ${err.message}`);
        button.disabled = false;
    }
}

async function toggleRole(button) {
    const { userId, role } = button.dataset;
    button.disabled = true;
    try {
        const changed = await applyRoleChange(userId, role);
        if (!changed) { button.disabled = false; return; }
        await loadUsers();
        if (openUserId === userId) await loadUserDetail(userId);
    } catch (err) {
        showAdminError(`Rolul nu a putut fi schimbat: ${err.message}`);
        button.disabled = false;
    }
}

async function loadUserDetail(userId) {
    const panel = document.getElementById('admin-user-detail');

    let user;
    try {
        user = await apiFetch(`/admin/users/${encodeURIComponent(userId)}`);
    } catch (err) {
        showAdminError(`Detaliile nu au putut fi încărcate: ${err.message}`);
        return;
    }

    const field = (label, value) => `
        <div class="admin-detail-field">
            <span class="label">${esc(label)}</span>
            <span>${esc(value || '—')}</span>
        </div>`;

    const accounts = user.accounts.length
        ? user.accounts.map((a) => `
            <tr>
                <td>${esc(a.name)}</td>
                <td>${esc(a.iban || '—')}</td>
                <td class="nowrap">${esc(a.status)}</td>
                <td class="num">${esc(formatMoney(a.balance_minor, a.currency))}</td>
            </tr>`).join('')
        : '<tr><td class="admin-empty" colspan="4">Niciun cont.</td></tr>';

    // last4 only - the API never sends the full number, CVV or expiry.
    const cards = user.cards.length
        ? user.cards.map((c) => `
            <tr>
                <td class="nowrap">•••• ${esc(c.last4 || '????')}</td>
                <td class="nowrap"><span class="admin-badge ${esc(c.status)}">${esc(c.status)}</span></td>
                <td class="num">${c.spending_limit_minor === null || c.spending_limit_minor === undefined
                    ? '—' : esc(formatMoney(c.spending_limit_minor, 'RON'))}</td>
            </tr>`).join('')
        : '<tr><td class="admin-empty" colspan="3">Niciun card.</td></tr>';

    const blocked = Boolean(user.blocked_at);
    const isSelf = user.id === currentAdminId;

    panel.innerHTML = `
        <div class="section-header-row">
            <h2 class="section-title">
                ${esc(user.first_name)} ${esc(user.last_name)}
                ${blocked
                    ? `<span class="admin-badge cancelled">blocat din ${esc(formatDateTime(user.blocked_at))}</span>`
                    : '<span class="admin-badge delivered">activ</span>'}
            </h2>
            <div class="admin-row-actions">
                <button class="admin-mini-btn" id="detail-block-btn"
                        ${isSelf ? 'disabled title="Nu te poți bloca singur"' : ''}>
                    ${blocked ? 'Deblochează contul' : 'Blochează contul'}
                </button>
                <button class="admin-mini-btn" id="detail-role-btn"
                        ${isSelf ? 'disabled title="Nu îți poți schimba propriul rol"' : ''}>
                    ${user.role === 'admin' ? 'Retrogradează la utilizator' : 'Fă administrator'}
                </button>
            </div>
        </div>
        <div class="admin-detail-grid">
            ${field('Email', user.email)}
            ${field('Telefon', user.phone)}
            ${field('CNP', user.national_id)}
            ${field('Adresă', user.address)}
            ${field('Rol', user.role)}
            ${field('Creat', formatDateTime(user.created_at))}
        </div>
        <h3 class="admin-subheading">Trimite document spre semnare</h3>
        <form id="admin-send-document-form" class="admin-send-document-form">
            <input type="text" class="field-input" id="admin-doc-title" placeholder="Titlu document" maxlength="200" required>
            <textarea class="field-input" id="admin-doc-body" placeholder="Conținutul documentului..." maxlength="3000" rows="4" required></textarea>
            <button type="submit" class="admin-mini-btn">Generează și trimite</button>
            <p class="field-hint" id="admin-send-document-status" hidden></p>
        </form>
        <div class="admin-table-wrap">
            <table class="admin-table">
                <thead><tr><th>Document</th><th>Trimis</th><th>Stare</th><th></th></tr></thead>
                <tbody id="admin-sent-documents-body"></tbody>
            </table>
        </div>
        <h3 class="admin-subheading">Conturi</h3>
        <div class="admin-table-wrap">
            <table class="admin-table">
                <thead><tr><th>Nume</th><th>IBAN</th><th>Stare</th><th class="num">Sold</th></tr></thead>
                <tbody>${accounts}</tbody>
            </table>
        </div>
        <h3 class="admin-subheading">Carduri</h3>
        <div class="admin-table-wrap">
            <table class="admin-table">
                <thead><tr><th>Card</th><th>Stare</th><th class="num">Limită</th></tr></thead>
                <tbody>${cards}</tbody>
            </table>
        </div>
        <div class="section-header-row">
            <h3 class="admin-subheading">Tranzacții</h3>
            <select class="field-input admin-search" id="admin-tx-card">
                <option value="">Toate cardurile</option>
                ${user.cards.map((c) =>
                    `<option value="${esc(c.id)}">•••• ${esc(c.last4 || '????')}</option>`).join('')}
            </select>
        </div>
        <div class="admin-table-wrap">
            <table class="admin-table" id="admin-tx-table">
                <thead>
                    <tr><th>Când</th><th>Descriere</th><th>Cont</th><th>Card</th><th class="num">Sumă</th></tr>
                </thead>
                <tbody></tbody>
            </table>
        </div>`;
    panel.hidden = false;
    openUserId = userId;

    if (!isSelf) {
        document.getElementById('detail-block-btn').addEventListener('click', async (e) => {
            e.target.disabled = true;
            try {
                if (await applyBlockChange(userId, blocked)) {
                    await loadUsers();
                    await loadUserDetail(userId);
                } else {
                    e.target.disabled = false;
                }
            } catch (err) {
                showAdminError(`Starea contului nu a putut fi schimbată: ${err.message}`);
                e.target.disabled = false;
            }
        });
        document.getElementById('detail-role-btn').addEventListener('click', async (e) => {
            e.target.disabled = true;
            try {
                if (await applyRoleChange(userId, user.role)) {
                    await loadUsers();
                    await loadUserDetail(userId);
                } else {
                    e.target.disabled = false;
                }
            } catch (err) {
                showAdminError(`Rolul nu a putut fi schimbat: ${err.message}`);
                e.target.disabled = false;
            }
        });
    }

    document.getElementById('admin-send-document-form').addEventListener('submit', async (e) => {
        e.preventDefault();
        const statusEl = document.getElementById('admin-send-document-status');
        const submitBtn = e.target.querySelector('button[type="submit"]');
        const title = document.getElementById('admin-doc-title').value.trim();
        const body = document.getElementById('admin-doc-body').value.trim();
        if (!title || !body) return;

        submitBtn.disabled = true;
        statusEl.hidden = true;
        try {
            const doc = await apiFetch(`/admin/users/${encodeURIComponent(userId)}/documents`, {
                method: 'POST',
                body: JSON.stringify({ title, body }),
            });
            e.target.reset();
            statusEl.hidden = false;
            statusEl.className = 'field-hint';
            statusEl.textContent = `Document trimis: ${doc.filename}`;
            await loadSentDocuments(userId);
        } catch (err) {
            statusEl.hidden = false;
            statusEl.className = 'field-hint ocr-warning';
            statusEl.textContent = `Trimiterea a eșuat: ${err.message}`;
        } finally {
            submitBtn.disabled = false;
        }
    });

    document.getElementById('admin-tx-card')
        .addEventListener('change', (e) => loadUserTransactions(userId, e.target.value));
    await loadUserTransactions(userId, '');
    await loadSentDocuments(userId);

    panel.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

async function loadUserTransactions(userId, cardId) {
    // A slow response for a previously-selected user must not overwrite the
    // table of whoever is open now.
    if (userId !== openUserId) return;

    const table = document.getElementById('admin-tx-table');
    if (!table) return;

    const params = new URLSearchParams({ limit: ADMIN_PAGE_SIZE });
    if (cardId) params.set('card_id', cardId);

    let entries;
    try {
        entries = await apiFetch(
            `/admin/users/${encodeURIComponent(userId)}/transactions?${params}`);
    } catch (err) {
        showAdminError(`Tranzacțiile nu au putut fi încărcate: ${err.message}`);
        return;
    }
    if (userId !== openUserId) return;

    if (!entries.length) {
        emptyRow(table, 5, cardId
            ? 'Nicio tranzacție înregistrată pe acest card.'
            : 'Nicio tranzacție.');
        return;
    }

    table.querySelector('tbody').innerHTML = entries
        .map((t) => {
            const outgoing = t.direction === 'debit';
            const amount = `${outgoing ? '-' : '+'} ${formatMoney(t.amount_minor, t.currency)}`;
            return `
            <tr>
                <td class="nowrap">${esc(formatDateTime(t.created_at))}</td>
                <td>${esc(t.description || '—')}</td>
                <td>${esc(t.account_name || '—')}</td>
                <td class="nowrap">${t.card_last4
                    ? `•••• ${esc(t.card_last4)}`
                    : '<span class="admin-empty">fără card</span>'}</td>
                <td class="num" style="color:${outgoing ? 'var(--expense-red)' : 'var(--income-green)'}">
                    ${esc(amount)}
                </td>
            </tr>`;
        })
        .join('');
}

async function loadSentDocuments(userId) {
    if (userId !== openUserId) return;

    const table = document.getElementById('admin-sent-documents-body')?.closest('table');
    if (!table) return;

    let documents;
    try {
        documents = await apiFetch(`/admin/users/${encodeURIComponent(userId)}/documents`);
    } catch (err) {
        showAdminError(`Documentele trimise nu au putut fi încărcate: ${err.message}`);
        return;
    }
    if (userId !== openUserId) return;

    if (!documents.length) {
        emptyRow(table, 4, 'Niciun document trimis încă.');
        return;
    }

    const tbody = table.querySelector('tbody');
    tbody.innerHTML = documents.map((doc) => `
        <tr>
            <td>${esc(doc.filename)}</td>
            <td class="nowrap">${esc(formatDateTime(doc.created_at))}</td>
            <td class="nowrap">${doc.signed
                ? '<span class="admin-badge delivered">semnat</span>'
                : '<span class="admin-badge">nesemnat</span>'}</td>
            <td class="nowrap"><button type="button" class="admin-mini-btn" data-doc-id="${esc(doc.id)}">Previzualizează</button></td>
        </tr>`).join('');

    tbody.querySelectorAll('button[data-doc-id]').forEach((btn) => {
        btn.addEventListener('click', () => previewDocumentPdf(`/admin/documents/${btn.dataset.docId}/pdf`));
    });
}

/** Opens a document's PDF in a new tab - fetched as an authenticated blob,
 * same reasoning as previewDocumentPdf() in app.js (a plain link would
 * cross origins without reliably carrying the session cookie). */
async function previewDocumentPdf(path) {
    try {
        const res = await fetch(`${API_BASE_URL}${path}`, { credentials: 'include' });
        if (!res.ok) throw new Error('Previzualizarea documentului a eșuat.');
        const blob = await res.blob();
        window.open(URL.createObjectURL(blob), '_blank');
    } catch (err) {
        showAdminError(err.message || 'Previzualizarea documentului a eșuat.');
    }
}

// ---------------------------------------------------------------------------
// Comenzi carduri - the panel's only write
// ---------------------------------------------------------------------------

const NEXT_STATUS = { pending: 'shipped', shipped: 'delivered' };

async function loadCardOrders() {
    const table = document.getElementById('admin-orders-table');
    const status = document.getElementById('admin-order-status').value;
    const params = new URLSearchParams({ limit: ADMIN_PAGE_SIZE });
    if (status) params.set('status', status);

    let orders;
    try {
        orders = await apiFetch(`/admin/card-orders?${params}`);
    } catch (err) {
        showAdminError(`Comenzile nu au putut fi încărcate: ${err.message}`);
        return;
    }

    if (!orders.length) {
        emptyRow(table, 5, 'Nicio comandă.');
        return;
    }

    table.querySelector('tbody').innerHTML = orders
        .map((o) => {
            const next = NEXT_STATUS[o.status];
            const actions = next
                ? `<button class="admin-mini-btn" data-order-id="${esc(o.id)}" data-next="${esc(next)}">
                       ${next === 'shipped' ? 'Marchează expediată' : 'Marchează livrată'}
                   </button>
                   <button class="admin-mini-btn" data-order-id="${esc(o.id)}" data-next="cancelled">
                       Anulează
                   </button>`
                : '<span class="admin-empty">—</span>';
            return `
                <tr>
                    <td>${esc(o.full_name)}<br><span class="admin-empty">${esc(o.user_email || '—')}</span></td>
                    <td>${esc(o.address)}, ${esc(o.city)} ${esc(o.postal_code)}, ${esc(o.country)}</td>
                    <td class="nowrap"><span class="admin-badge ${esc(o.status)}">${esc(o.status)}</span></td>
                    <td class="nowrap">${esc(formatDateTime(o.created_at))}</td>
                    <td class="nowrap"><div class="admin-row-actions">${actions}</div></td>
                </tr>`;
        })
        .join('');

    table.querySelectorAll('button[data-order-id]').forEach((button) => {
        button.addEventListener('click', () => updateOrderStatus(button));
    });
}

async function updateOrderStatus(button) {
    const { orderId, next } = button.dataset;
    if (next === 'cancelled' &&
        !window.confirm('Sigur vrei să anulezi această comandă de card?')) {
        return;
    }

    // Disable the whole row's buttons, not just the clicked one: a
    // double-click on the sibling would fire a second transition.
    const row = button.closest('tr');
    row.querySelectorAll('button').forEach((b) => { b.disabled = true; });

    try {
        await apiFetch(`/admin/card-orders/${encodeURIComponent(orderId)}`, {
            method: 'PATCH',
            body: JSON.stringify({ status: next }),
        });
        await loadCardOrders();
    } catch (err) {
        showAdminError(`Starea comenzii nu a putut fi schimbată: ${err.message}`);
        row.querySelectorAll('button').forEach((b) => { b.disabled = false; });
    }
}

// ---------------------------------------------------------------------------
// Jurnal audit
// ---------------------------------------------------------------------------

async function loadAuditLog() {
    const table = document.getElementById('admin-audit-table');
    const action = document.getElementById('admin-audit-filter').value.trim();
    const params = new URLSearchParams({ limit: ADMIN_PAGE_SIZE });
    if (action) params.set('action', action);

    let entries;
    try {
        entries = await apiFetch(`/admin/audit-log?${params}`);
    } catch (err) {
        showAdminError(`Jurnalul nu a putut fi încărcat: ${err.message}`);
        return;
    }

    if (!entries.length) {
        emptyRow(table, 4, action ? 'Niciun rezultat.' : 'Jurnal gol.');
        return;
    }

    table.querySelector('tbody').innerHTML = entries
        .map((e) => `
            <tr>
                <td class="nowrap">${esc(formatDateTime(e.created_at))}</td>
                <td>${esc(e.action)}</td>
                <td>${esc(e.entity)}</td>
                <td>${esc(e.user_id || '—')}</td>
            </tr>`)
        .join('');
}
