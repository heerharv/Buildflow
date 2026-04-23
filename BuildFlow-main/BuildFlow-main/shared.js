// shared.js — injected into every page
// Keep a single host identity so auth cookies do not split across 127.0.0.1/localhost.
(function enforceCanonicalHost() {
    if (window.location.hostname === '127.0.0.1') {
        const target = `http://localhost${window.location.port ? `:${window.location.port}` : ''}${window.location.pathname}${window.location.search}${window.location.hash}`;
        window.location.replace(target);
    }
})();

const API_BASE = window.location.origin + '/api';

const NAV_ITEMS = [
    { id: 'dashboard', label: '⬛ Dashboard', href: 'dashboard.html' },
    { id: 'insights',  label: '⚡ Insights',  href: 'insights.html' },
    { id: 'specs',     label: '📝 Specs',    href: 'specs.html' },
    { id: 'signals',   label: '📥 Signals',   href: 'signals.html' },
    { id: 'kanban',    label: '⬜ Kanban',    href: 'kanban.html' },
    { id: 'timeline',  label: '⬜ Timeline',   href: 'timeline.html' },
    { id: 'churn',     label: '↺ Churn',      href: 'churn.html' },
    { id: 'shares',    label: '🔗 Shares',     href: 'shares.html' },
    { id: 'risk',      label: '⚠ Risk',       href: 'risk.html' },
    { id: 'settings',  label: '⚙ Settings',   href: 'settings.html' },
];

function buildSidebar(activePage) {
    const sidebar = document.getElementById('sidebar');
    if (!sidebar) return;

    sidebar.innerHTML = `
        <div class="brand">
            <span class="brand-logo">Build Flow</span>
            <span class="brand-tag">Risk Intel</span>
        </div>
        <nav class="nav">
            ${NAV_ITEMS.map(n => `
                <a href="/${n.href}" class="nav-item ${activePage === n.id ? 'active' : ''}">
                    ${n.label}
                </a>
            `).join('')}
        </nav>
        <div class="sidebar-footer">
            <div class="user-badge">
                <div class="user-avatar" id="userAvatar">?</div>
                <div class="user-info">
                    <div class="user-name" id="sidebarUsername">—</div>
                    <div class="jira-status" id="sidebarJira">Jira: not configured</div>
                </div>
            </div>
            <button class="logout-btn" onclick="doLogout()">Sign out</button>
        </div>
    `;
}

async function initPage(activePage) {
    buildSidebar(activePage);
    try {
        const r = await fetch(`${API_BASE}/auth/me`, { credentials: 'include' });
        if (!r.ok) { window.location.href = '/index.html'; return null; }
        const { user } = await r.json();
        const nameEl = document.getElementById('sidebarUsername');
        const avatarEl = document.getElementById('userAvatar');
        const jiraEl = document.getElementById('sidebarJira');
        if (nameEl) nameEl.textContent = user.username;
        if (avatarEl) avatarEl.textContent = user.username[0].toUpperCase();
        if (jiraEl) jiraEl.textContent = user.has_jira ? 'Jira: connected ✓' : 'Jira: not configured';
        if (jiraEl && user.has_jira) jiraEl.style.color = 'var(--success)';
        return user;
    } catch {
        window.location.href = '/index.html';
        return null;
    }
}

async function doLogout() {
    await fetch(`${API_BASE}/auth/logout`, { method: 'POST', credentials: 'include' });
    window.location.href = '/index.html';
}

// Populate project selects
async function loadProjects(selectId, onSelect) {
    try {
        const r = await fetch(`${API_BASE}/jira/projects`, { credentials: 'include' });
        if (!r.ok) return;
        const { projects, mock_mode } = await r.json();
        const sel = document.getElementById(selectId);
        if (!sel) return;
        const jiraEl = document.getElementById('sidebarJira');
        if (jiraEl && mock_mode) {
            jiraEl.textContent = 'Jira: mock mode ✓';
            jiraEl.style.color = 'var(--accent)';
        }
        projects.forEach(p => {
            const opt = document.createElement('option');
            opt.value = p.key;
            opt.textContent = `${p.key} — ${p.name}`;
            sel.appendChild(opt);
        });
        if (onSelect) sel.addEventListener('change', () => onSelect());
        if (projects.length > 0) {
            sel.value = projects[0].key;
            if (onSelect) await onSelect();
        }
        return projects;
    } catch { return []; }
}
