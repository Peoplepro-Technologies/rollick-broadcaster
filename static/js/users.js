// Users page — search, add modal, delete, Excel import.

function filterUsers() {
  const q = document.getElementById('user-search').value.toLowerCase();
  document.querySelectorAll('#users-tbody tr').forEach(tr => {
    const s = tr.dataset.search || '';
    tr.style.display = s.includes(q) ? '' : 'none';
  });
}

function openAddUser() {
  document.getElementById('add-user-modal').hidden = false;
  document.getElementById('add-user-form').reset();
  document.getElementById('add-user-error').hidden = true;
}
function closeAddUser() {
  document.getElementById('add-user-modal').hidden = true;
}

async function submitAddUser(ev) {
  ev.preventDefault();
  const form = ev.target;
  const fd = new FormData(form);
  const payload = {
    name: fd.get('name'),
    phone: fd.get('phone'),
    email: fd.get('email') || null,
    department: fd.get('department') || null,
    location: fd.get('location') || null,
    is_active: !!fd.get('is_active'),
  };
  const errEl = document.getElementById('add-user-error');
  errEl.hidden = true;
  try {
    const r = await fetch('/api/users', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (!r.ok) {
      const body = await r.json().catch(() => ({}));
      errEl.textContent = humanizeError(body.detail, r.status);
      errEl.hidden = false;
      return;
    }
    location.reload();
  } catch (e) {
    errEl.textContent = 'Network error: ' + e.message;
    errEl.hidden = false;
  }
}

async function deleteUser(id, name) {
  if (!confirm(`Delete ${name}? This cannot be undone.`)) return;
  const r = await fetch(`/api/users/${id}`, { method: 'DELETE' });
  if (r.ok) location.reload();
  else alert('Delete failed: ' + r.status);
}

// ── Excel import: error modal + auto-reload ─────────────────────────────

// Single source of truth — keep in sync with
// broadcaster/services/users.py::ERROR_HUMAN.
const ERROR_HUMAN = {
  name_or_phone_missing:     'Name and phone are required.',
  invalid_phone_format:      'Phone must be 10 digits (Indian mobile).',
  invalid_email_format:      'Email format is invalid.',
  duplicate_phone_in_file:   'Duplicate phone in uploaded file.',
  duplicate_email_in_file:   'Duplicate email in uploaded file.',
  duplicate_email_in_db:     'Email already exists for another user.',
  phone_taken:               'Phone already exists; skipped (upsert off).',
  db_error:                  'Database error while saving row.',
};
function humanizeReason(reason) {
  if (typeof reason !== 'string') return 'Unknown reason';
  // db_error carries the message as suffix — strip for humanize().
  const key = reason.startsWith('db_error') ? 'db_error' : reason;
  return ERROR_HUMAN[key] || reason;
}

let _importErrorsBody = null;

function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  })[c]);
}

function openImportErrorsModal(body) {
  _importErrorsBody = body;
  const rows = body.errors || [];
  const imported = (body.inserted || 0) + (body.updated || 0);
  const truncated = body.errors_truncated ? ` Showing first ${rows.length}; download the CSV for the full list.` : '';
  document.getElementById('import-errors-summary').textContent =
    `${body.skipped || rows.length} row${(body.skipped || rows.length) === 1 ? '' : 's'} skipped, ${imported} row${imported === 1 ? '' : 's'} imported. Click Close to refresh the user list.${truncated}`;
  const tbody = document.getElementById('import-errors-tbody');
  tbody.innerHTML = '';
  for (const e of rows) {
    const tr = document.createElement('tr');
    tr.innerHTML =
      `<td>${escapeHtml(e.row ?? '')}</td>` +
      `<td>${escapeHtml(e.field ?? '')}</td>` +
      `<td>${escapeHtml(e.value ?? '')}</td>` +
      `<td>${escapeHtml(humanizeReason(e.reason))}</td>`;
    tbody.appendChild(tr);
  }
  document.getElementById('import-errors-modal').hidden = false;
}

function closeImportErrorsModal(reload) {
  document.getElementById('import-errors-modal').hidden = true;
  if (reload) location.reload();
}

document.addEventListener('keydown', (ev) => {
  if (ev.key === 'Escape') {
    const m = document.getElementById('import-errors-modal');
    if (m && !m.hidden) closeImportErrorsModal(false);
  }
});

document.getElementById('import-errors-download').addEventListener('click', async () => {
  if (!_importErrorsBody || !_importErrorsBody.errors || !_importErrorsBody.errors.length) return;
  try {
    const r = await fetch('/api/users/upload-excel/errors.csv', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ errors: _importErrorsBody.errors }),
    });
    if (!r.ok) {
      alert('Download failed: ' + r.status);
      return;
    }
    const blob = await r.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    // Use the server-provided filename from Content-Disposition if possible.
    const cd = r.headers.get('Content-Disposition') || '';
    const m = cd.match(/filename="([^"]+)"/);
    a.download = m ? m[1] : 'users_import_errors.csv';
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  } catch (e) {
    alert('Network error: ' + e.message);
  }
});

// Excel upload — wires the hidden <input type="file">.
// EVERY upload overwrites the user list (admin preserved). Confirm
// before submit so the destructive action isn't silent.
document.getElementById('xlsx-input').addEventListener('change', async (ev) => {
  const file = ev.target.files[0];
  if (!file) return;
  // Destructive-replace confirm. Counts known to user via header subtitle.
  const ok = window.confirm(
    'Replace the user list with this file?\n\n' +
    'Existing users whose phone is NOT in the file will be deleted.\n' +
    'The admin account is always preserved.\n\n' +
    'Continue?'
  );
  if (!ok) {
    ev.target.value = '';   // clear so re-selecting same file fires change
    return;
  }

  const fd = new FormData();
  fd.append('file', file);
  const result = document.getElementById('import-result');
  const msg = document.getElementById('import-result-msg');
  const viewBtn = document.getElementById('import-result-view-errors');

  // Reset banner to "loading" state.
  result.classList.remove('form-error');
  result.classList.add('form-success');
  msg.textContent = `Uploading ${file.name}…`;
  viewBtn.hidden = true;
  result.hidden = false;
  try {
    const r = await fetch('/api/users/upload-excel', { method: 'POST', body: fd });
    const body = await r.json().catch(() => ({}));
    if (r.ok) {
      const parts = [
        `+${body.inserted} added`,
        `~${body.updated} updated`,
        `!${body.skipped} skipped`,
        `−${body.deleted || 0} removed`,
      ];
      let txt = `✓ Import complete — ${parts.join(', ')}.`;
      if ((body.deleted || 0) > 0) {
        txt += ` ${body.deleted} user${body.deleted === 1 ? '' : 's'} not in the file were deleted (admin preserved).`;
      }
      if (body.skipped > 0) {
        txt += ` ${body.skipped} row${body.skipped === 1 ? '' : 's'} had errors.`;
      }
      msg.textContent = txt;
      // Show "View errors" only when there are skipped rows.
      if (body.skipped > 0) {
        viewBtn.hidden = false;
        viewBtn.onclick = () => openImportErrorsModal(body);
      } else {
        viewBtn.hidden = true;
      }
      // Auto-reload strategy:
      //   inserted+updated > 0, skipped == 0  -> reload immediately (clean import)
      //   inserted+updated > 0, skipped  > 0  -> open modal; Close button reloads
      //   inserted+updated == 0, skipped > 0 -> open modal; no reload (nothing to refresh)
      //   deleted > 0 alone -> reload so the user sees the new list
      const changed = (body.inserted || 0) + (body.updated || 0);
      const lost    = body.deleted || 0;
      if (body.skipped > 0) {
        openImportErrorsModal(body);
      } else if (changed > 0 || lost > 0) {
        location.reload();
      }
    } else {
      result.classList.remove('form-success');
      result.classList.add('form-error');
      msg.textContent = '✗ Import failed: ' + (body.detail || r.status);
    }
  } catch (e) {
    result.classList.remove('form-success');
    result.classList.add('form-error');
    msg.textContent = '✗ Network error: ' + e.message;
  }
  ev.target.value = '';
});

function humanizeError(detail, status) {
  if (!detail) return `Error ${status}`;
  const map = {
    invalid_phone: 'Phone must be exactly 10 digits.',
    invalid_email: 'Email format is invalid.',
    phone_taken: 'A user with that phone already exists.',
    name_required: 'Name is required.',
  };
  return map[detail] || `${detail} (${status})`;
}
