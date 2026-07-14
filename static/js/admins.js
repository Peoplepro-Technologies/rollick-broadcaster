// /static/js/admins.js — super_admin roster.
//
// Pattern mirrors /static/js/users.js. Reads identity from a server-
// injected <script type="application/json" id="current-admin"> block,
// fetches /api/admins for the table, and uses /api/admins/* for
// mutations.

const ADMINS = (() => {
  const el = document.getElementById('current-admin');
  if (!el) throw new Error("current-admin block not found");
  return { me: JSON.parse(el.textContent) };
})();

// ── Utilities ─────────────────────────────────────────────────────────

function closeModal(id) {
  document.getElementById(id).hidden = true;
  document.querySelectorAll(`#${id} .form-error, #${id} .form-success`)
    .forEach(e => { e.hidden = true; e.textContent = ''; });
}

function showError(modalId, bannerId, msg) {
  const e = document.getElementById(bannerId);
  if (!e) return;
  e.textContent = msg;
  e.hidden = false;
}

function flashSuccess(el, msg, ms = 3000) {
  el.textContent = msg;
  el.hidden = false;
  setTimeout(() => { el.hidden = true; el.textContent = ''; }, ms);
}

function generatePassword(targetId) {
  const alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789";
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  let pw = "";
  for (const b of bytes) pw += alphabet[b % alphabet.length];
  const el = document.getElementById(targetId);
  el.value = pw;
  el.dispatchEvent(new Event('input'));
}

async function fetchAdmins() {
  const r = await fetch('/api/admins');
  if (!r.ok) throw new Error('admins fetch failed');
  return r.json();
}

function escapeAttr(s) {
  return String(s).replace(/[&<>"']/g, c =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c]);
}

function rowHtml(a) {
  const recoveryEmail = a.recovery_email
    ? escapeAttr(a.recovery_email)
    : '<span class="muted">— (fallback)</span>';
  return `
    <tr data-admin-id="${a.id}" data-admin-role="${a.role}"
        data-admin-username="${a.username}"
        data-admin-recovery-email="${escapeAttr(a.recovery_email || '')}">
      <td>${escapeAttr(a.username)}</td>
      <td><span class="pill ${a.role}">${a.role}</span></td>
      <td>${recoveryEmail}</td>
      <td>${a.created_at || '—'}</td>
      <td>
        <button class="btn small" onclick="openRoleModal(${a.id}, '${escapeAttr(a.username)}', '${a.role}')">Change role</button>
        <button class="btn small secondary" onclick="openPasswordModal(${a.id}, '${escapeAttr(a.username)}')">Change password</button>
        <button class="btn small secondary" onclick="openRecoveryEmailModal(${a.id}, '${escapeAttr(a.username)}', '${escapeAttr(a.recovery_email || '')}')">Recovery email</button>
        <button class="btn small" onclick="openSendRecoveryMailModal(${a.id}, '${escapeAttr(a.username)}', '${escapeAttr(a.recovery_email || '')}')">Send recovery mail</button>
        <button class="btn small danger" onclick="openDeleteModal(${a.id}, '${escapeAttr(a.username)}')">Delete</button>
      </td>
    </tr>
  `;
}

async function reloadAdminsTable() {
  const list = await fetchAdmins();
  const tbody = document.getElementById('admins-tbody');
  tbody.innerHTML = list.length
    ? list.map(rowHtml).join('')
    : '<tr><td colspan="5" class="empty">No admins.</td></tr>';
  applyLockoutFlags(list);
}

// ── Lockout flags ─────────────────────────────────────────────────────

function applyLockoutFlags(admins) {
  const me = ADMINS.me;
  const supers = admins.filter(a => a.role === 'super_admin');
  for (const tr of document.querySelectorAll('#admins-tbody tr')) {
    const username = tr.dataset.adminUsername;
    const role = tr.dataset.adminRole;
    const isSelf = username === me.username;
    for (const btn of tr.querySelectorAll('button')) {
      btn.disabled = isSelf;
      btn.title = isSelf
        ? "You can't manage your own account from the roster — use 'Your account' above."
        : '';
    }
    const isOnlySuper = role === 'super_admin' && supers.length === 1;
    if (isOnlySuper && !isSelf) {
      const del = tr.querySelector('button.danger');
      if (del) {
        del.disabled = true;
        del.title = 'This is the last super_admin — cannot delete.';
      }
    }
  }
}

// ── Modal openers ──────────────────────────────────────────────────────

function openAddAdmin() {
  document.getElementById('add-admin-form').reset();
  closeModal('add-admin-modal');
  document.getElementById('add-admin-modal').hidden = false;
}

function openRoleModal(adminId, username, currentRole) {
  document.getElementById('role-admin-id').value = adminId;
  document.getElementById('role-username').textContent = username;
  document.getElementById('role-select').value = currentRole;
  document.getElementById('role-modal').hidden = false;
}

function openPasswordModal(adminId, username) {
  document.getElementById('password-admin-id').value = adminId;
  document.getElementById('password-username').textContent = username;
  document.getElementById('password-input').value = '';
  document.getElementById('password-confirm').value = '';
  document.getElementById('password-submit').disabled = true;
  document.getElementById('password-modal').hidden = false;
}

function openDeleteModal(adminId, username) {
  document.getElementById('delete-admin-id').value = adminId;
  document.getElementById('delete-username').textContent = username;
  document.getElementById('delete-modal').hidden = false;
}

function openRecoveryEmailModal(adminId, username, currentEmail) {
  document.getElementById('recovery-email-admin-id').value = adminId;
  document.getElementById('recovery-email-username').textContent = username;
  // Pre-populate so super_admin sees the existing value rather than
  // starting from a blank form (which they'd then have to retype).
  document.getElementById('recovery-email-input').value = currentEmail || '';
  document.getElementById('recovery-email-modal').hidden = false;
}

function openSelfPasswordModal() {
  document.getElementById('self-pw-input').value = '';
  document.getElementById('self-pw-confirm').value = '';
  document.getElementById('self-pw-submit').disabled = true;
  document.getElementById('self-pw-modal').hidden = false;
}

// ── Form submissions ──────────────────────────────────────────────────

async function submitAddAdmin(ev) {
  ev.preventDefault();
  const fd = new FormData(ev.target);
  const payload = Object.fromEntries(fd.entries());
  const r = await fetch('/api/admins', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) {
    showError('add-admin-modal', 'add-admin-error',
              body.detail || `Error ${r.status}`);
    return;
  }
  closeModal('add-admin-modal');
  await reloadAdminsTable();
  flashSuccess(document.getElementById('self-pw-success'),
               `Created admin '${body.username}'.`);
}

async function submitRole(ev) {
  ev.preventDefault();
  const fd = new FormData(ev.target);
  const adminId = Number(fd.get('admin_id'));
  const role = fd.get('role');
  const r = await fetch(`/api/admins/${adminId}/role`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ role }),
  });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) {
    showError('role-modal', 'role-error', body.detail || `Error ${r.status}`);
    return;
  }
  closeModal('role-modal');
  await reloadAdminsTable();
}

async function submitPassword(ev) {
  ev.preventDefault();
  const fd = new FormData(ev.target);
  if (fd.get('password') !== fd.get('confirm')) {
    showError('password-modal', 'password-error', 'Passwords do not match.');
    return;
  }
  const adminId = Number(fd.get('admin_id'));
  const r = await fetch(`/api/admins/${adminId}/password`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ password: fd.get('password') }),
  });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) {
    showError('password-modal', 'password-error', body.detail || `Error ${r.status}`);
    return;
  }
  closeModal('password-modal');
  flashSuccess(document.getElementById('self-pw-success'),
               `Password updated.`);
}

async function submitDelete(ev) {
  ev.preventDefault();
  const fd = new FormData(ev.target);
  const adminId = Number(fd.get('admin_id'));
  const r = await fetch(`/api/admins/${adminId}`, { method: 'DELETE' });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) {
    showError('delete-modal', 'delete-error', body.detail || `Error ${r.status}`);
    return;
  }
  closeModal('delete-modal');
  await reloadAdminsTable();
}

async function submitSelfPassword(ev) {
  ev.preventDefault();
  const fd = new FormData(ev.target);
  if (fd.get('password') !== fd.get('confirm')) {
    showError('self-pw-modal', 'self-pw-modal-error', 'Passwords do not match.');
    return;
  }
  const r = await fetch(`/api/admins/${ADMINS.me.id}/password`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ password: fd.get('password') }),
  });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) {
    showError('self-pw-modal', 'self-pw-modal-error',
              body.detail || `Error ${r.status}`);
    return;
  }
  closeModal('self-pw-modal');
  flashSuccess(document.getElementById('self-pw-success'),
               'Password updated.');
}

async function submitRecoveryEmail(ev) {
  ev.preventDefault();
  const fd = new FormData(ev.target);
  const adminId = Number(fd.get('admin_id'));
  const r = await fetch(`/api/admins/${adminId}/recovery-email`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ recovery_email: fd.get('recovery_email') }),
  });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) {
    showError('recovery-email-modal', 'recovery-email-error',
              body.detail || `Error ${r.status}`);
    return;
  }
  closeModal('recovery-email-modal');
  await reloadAdminsTable();
  flashSuccess(document.getElementById('self-pw-success'),
               'Recovery email updated.');
}

function openSendRecoveryMailModal(adminId, username, recoveryEmail) {
  document.getElementById('send-recovery-admin-id').value = adminId;
  document.getElementById('send-recovery-username').textContent = username;
  // Show the resolved recipient (the row's per-admin email, or a
  // fallback notice when empty). The server is the source of truth —
  // we render the pre-computed string here, and the submission path
  // will give us the actual recipient the email routed to.
  const recipientEl = document.getElementById('send-recovery-recipient');
  if (recoveryEmail) {
    recipientEl.textContent = recoveryEmail;
    recipientEl.classList.remove('muted');
  } else {
    recipientEl.textContent = '(global fallback — see /admin/settings)';
    recipientEl.classList.add('muted');
  }
  document.getElementById('send-recovery-modal').hidden = false;
}

async function submitSendRecoveryMail(ev) {
  ev.preventDefault();
  const fd = new FormData(ev.target);
  const adminId = Number(fd.get('admin_id'));
  const r = await fetch(`/api/admins/${adminId}/send-recovery-email`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
  });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) {
    showError('send-recovery-modal', 'send-recovery-error',
              body.detail || `Error ${r.status}`);
    return;
  }
  closeModal('send-recovery-modal');
  const msg = body.recipient
    ? `Recovery mail sent to ${body.recipient}.`
    : 'Recovery mail sent.';
  flashSuccess(document.getElementById('self-pw-success'), msg);
  // Force a table reload so the must_change_password flag, if it
  // changed, is reflected in any UI hooks downstream.
  await reloadAdminsTable();
}

// ── Confirm-password pair: enable submit only when both match. ─────────

function wireConfirmPair(inputId, confirmId, submitId) {
  const input = document.getElementById(inputId);
  const confirm = document.getElementById(confirmId);
  const submit = document.getElementById(submitId);
  function check() {
    const ok = input.value.length >= 8 && input.value === confirm.value;
    submit.disabled = !ok;
  }
  input.addEventListener('input', check);
  confirm.addEventListener('input', check);
}

document.addEventListener('DOMContentLoaded', () => {
  wireConfirmPair('password-input', 'password-confirm', 'password-submit');
  wireConfirmPair('self-pw-input', 'self-pw-confirm', 'self-pw-submit');

  fetchAdmins().then(applyLockoutFlags).catch(() => {});
});
