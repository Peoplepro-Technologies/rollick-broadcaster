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

// Excel upload — wires the hidden <input type="file">.
document.getElementById('xlsx-input').addEventListener('change', async (ev) => {
  const file = ev.target.files[0];
  if (!file) return;
  const fd = new FormData();
  fd.append('file', file);
  const result = document.getElementById('import-result');
  const msg = document.getElementById('import-result-msg');
  const reloadBtn = document.getElementById('import-result-reload');

  // Reset banner to "loading" state.
  result.classList.remove('form-error');
  result.classList.add('form-success');
  msg.textContent = `Uploading ${file.name}…`;
  reloadBtn.hidden = true;
  result.hidden = false;
  try {
    const r = await fetch('/api/users/upload-excel?upsert=true', { method: 'POST', body: fd });
    const body = await r.json().catch(() => ({}));
    if (r.ok) {
      const parts = [
        `+${body.inserted} added`,
        `~${body.updated} updated`,
        `!${body.skipped} skipped`,
      ];
      let txt = `✓ Import complete — ${parts.join(', ')}.`;
      if (body.errors && body.errors.length) {
        txt += ` ${body.errors.length} row${body.errors.length === 1 ? '' : 's'} skipped: `;
        txt += body.errors.slice(0, 3).map(e => `row ${e.row} (${e.reason})`).join('; ');
        if (body.errors.length > 3) txt += `, +${body.errors.length - 3} more`;
      }
      msg.textContent = txt;
      // Only offer reload if the table actually changed.
      if (body.inserted > 0 || body.updated > 0) {
        reloadBtn.hidden = false;
      }
    } else {
      // Real error: red banner, no reload button.
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
