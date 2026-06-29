// static/js/schedule.js
// ES module for broadcast scheduling UX.
// Depends on: native Intl, no other deps.

const DEFAULT_TZ = "Asia/Kolkata";

function nowInTz(tz) {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: tz, year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hour12: false,
  }).formatToParts(new Date());
  const get = (t) => parts.find(p => p.type === t).value;
  return { year: get("year"), month: get("month"), day: get("day"),
           hour: get("hour"), minute: get("minute") };
}

// ── Presets ──────────────────────────────────────────────────

function roundUpTo5(date) {
  const d = new Date(date);
  const m = d.getMinutes();
  const add = (5 - (m % 5)) % 5;
  d.setMinutes(m + add, 0, 0);
  if (add === 0) d.setMinutes(d.getMinutes() + 5);
  return d;
}

function formatLocal(date, tz) {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: tz, year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hour12: false,
  }).formatToParts(date);
  const get = (t) => parts.find(p => p.type === t).value;
  return `${get("year")}-${get("month")}-${get("day")}T${get("hour")}:${get("minute")}`;
}

function presetIn15(now = new Date(), tz = DEFAULT_TZ) {
  const d = new Date(now.getTime() + 15 * 60 * 1000);
  return formatLocal(roundUpTo5(d), tz);
}

function presetIn1Hour(now = new Date(), tz = DEFAULT_TZ) {
  const d = new Date(now.getTime() + 60 * 60 * 1000);
  return formatLocal(roundUpTo5(d), tz);
}

function presetTomorrow9(now = new Date(), tz = DEFAULT_TZ) {
  const ymd = nowInTz(tz);
  const d = new Date(`${ymd.year}-${ymd.month}-${ymd.day}T09:00:00`);
  d.setDate(d.getDate() + 1);
  return formatLocal(d, tz);
}

function presetNextMonday9(now = new Date(), tz = DEFAULT_TZ) {
  const target = new Date(now);
  const day = target.getDay(); // 0..6, Sun..Sat
  const daysUntilMon = ((1 - day + 7) % 7) || 7;
  target.setDate(target.getDate() + daysUntilMon);
  target.setHours(9, 0, 0, 0);
  return formatLocal(target, tz);
}

function localInputToIso(localStr) {
  // datetime-local has no TZ → interpret as local browser time, convert to ISO UTC
  return new Date(localStr).toISOString();
}

// ── Picker controller ───────────────────────────────────────

const PRESETS = [
  { id: "in15",  label: "+15 min",      compute: (n, tz) => presetIn15(n, tz) },
  { id: "in1h",  label: "+1 hour",      compute: (n, tz) => presetIn1Hour(n, tz) },
  { id: "tom9",  label: "Tomorrow 9am", compute: (n, tz) => presetTomorrow9(n, tz) },
  { id: "mon9",  label: "Next Mon 9am", compute: (n, tz) => presetNextMonday9(n, tz) },
];

export function initSchedulePicker(rootEl, opts = {}) {
  const tz = opts.tz || (Intl.DateTimeFormat().resolvedOptions().timeZone) || DEFAULT_TZ;
  const initial = opts.initial || formatLocal(roundUpTo5(new Date()), tz);

  const chipRow   = rootEl.querySelector(".chip-row");
  const customIn  = rootEl.querySelector('input[type="datetime-local"]');
  // submit-button may live OUTSIDE the picker block (e.g. .form-actions on the
  // compose form). Accept an explicit selector via opts, then look inside rootEl,
  // then fall back to a single document-wide match (one picker per page is
  // assumed).
  const submitBtn =
    (opts.submitSelector && document.querySelector(opts.submitSelector)) ||
    rootEl.querySelector("[data-submit-button]") ||
    document.querySelector("[data-submit-button]");
  const warningEl = rootEl.querySelector("[data-warning]");
  const tzHint    = rootEl.querySelector("[data-tz-hint]");

  customIn.value = initial;
  if (tzHint) {
    tzHint.textContent = tz === DEFAULT_TZ
      ? "Asia/Kolkata (IST, UTC+5:30)"
      : `Timezone: ${tz}`;
  }

  // Build preset chips
  chipRow.innerHTML = "";
  for (const p of PRESETS) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "chip";
    btn.dataset.preset = p.id;
    btn.textContent = p.label;
    btn.addEventListener("click", () => {
      const v = p.compute(new Date(), tz);
      customIn.value = v;
      const scheduleRadio = rootEl.querySelector('input[name="_schedule_mode"][value="schedule"]');
      if (scheduleRadio && !scheduleRadio.checked) scheduleRadio.checked = true;
      refresh();
    });
    chipRow.appendChild(btn);
  }

  customIn.addEventListener("input", refresh);
  for (const r of rootEl.querySelectorAll('input[name="_schedule_mode"]')) {
    r.addEventListener("change", refresh);
  }

  function selectedMode() {
    const r = rootEl.querySelector('input[name="_schedule_mode"]:checked');
    return r ? r.value : "schedule";
  }

  function getScheduledIso() {
    const m = selectedMode();
    if (m !== "schedule") return null;
    const v = customIn.value;
    if (!v) return null;
    return localInputToIso(v);
  }

  function validate() {
    const m = selectedMode();
    if (m !== "schedule") {
      warningEl.hidden = true;
      warningEl.textContent = "";
      submitBtn.disabled = false;
      return true;
    }
    const v = customIn.value;
    if (!v) {
      warningEl.hidden = true;
      warningEl.textContent = "";
      submitBtn.disabled = false;
      return false;
    }
    const ms = new Date(localInputToIso(v)).getTime() - Date.now();
    if (ms < 0) {
      warningEl.textContent = "Pick a time in the future.";
      warningEl.hidden = false;
      submitBtn.disabled = true;
      return false;
    }
    if (ms < 5 * 60 * 1000) {
      warningEl.textContent = "";
      const link = document.createElement("a");
      link.href = "#";
      link.textContent = "Switch to Send now";
      link.onclick = (e) => {
        e.preventDefault();
        const r = rootEl.querySelector('input[name="_schedule_mode"][value="send_now"]');
        if (r) r.checked = true;
        refresh();
      };
      warningEl.appendChild(link);
      warningEl.hidden = false;
      submitBtn.disabled = false;
      return true;
    }
    warningEl.textContent = "";
    warningEl.hidden = true;
    submitBtn.disabled = false;
    return true;
  }

  function updateSubmit() {
    const m = selectedMode();
    if (m === "send_now") {
      submitBtn.textContent = "Send to recipients";
    } else if (m === "draft") {
      submitBtn.textContent = "Save Draft";
    } else {
      const v = customIn.value;
      if (v) {
        const d = new Date(localInputToIso(v));
        const fmt = new Intl.DateTimeFormat("en-GB", {
          timeZone: tz, weekday: "short", day: "2-digit", month: "short",
          hour: "2-digit", minute: "2-digit", hour12: true, timeZoneName: "short",
        });
        submitBtn.textContent = `Schedule for ${fmt.format(d)}`;
      } else {
        submitBtn.textContent = "Schedule";
      }
    }
  }

  function refresh() {
    validate();
    updateSubmit();
  }

  refresh();

  return {
    tz,
    getMode: selectedMode,
    getScheduledIso,
    refresh,
  };
}

// ── List-view formatter ─────────────────────────────────────

export function formatScheduledForList(iso, now = new Date(), tz = DEFAULT_TZ) {
  if (!iso) return "—";
  const dt = new Date(iso);
  if (isNaN(dt)) return "—";
  const absDelta = Math.abs(dt - now);
  const future = dt > now;
  const fmt = new Intl.DateTimeFormat("en-GB", {
    timeZone: tz, day: "2-digit", month: "short", hour: "2-digit",
    minute: "2-digit", hour12: true, timeZoneName: "short",
  });
  const abs = fmt.format(dt);
  if (absDelta < 30 * 1000) return future ? "Just now — firing any second" : "Just fired";
  if (future && absDelta < 60 * 1000) return `in ${Math.round(absDelta/1000)} sec`;
  if (future && absDelta < 60 * 60 * 1000) return `in ${Math.round(absDelta/(60*1000))} min`;
  if (future && absDelta < 24 * 60 * 60 * 1000) {
    const h = Math.floor(absDelta / (60*60*1000));
    const m = Math.round((absDelta % (60*60*1000)) / (60*1000));
    return `${abs} · in ${h}h ${m}m`;
  }
  if (future) return `${abs} · in ${Math.round(absDelta/(24*60*60*1000))}d`;
  return `Sent at ${abs}`;
}

export function applyListFormatter(rootEl = document) {
  rootEl.querySelectorAll("[data-scheduled-at]").forEach((td) => {
    const iso = td.dataset.scheduledAt;
    td.textContent = formatScheduledForList(iso);
  });
}
