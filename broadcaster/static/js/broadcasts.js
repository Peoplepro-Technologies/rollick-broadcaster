// /admin/broadcasts search typeahead.
//
// Wire up the title-search input to fetch suggestions from
// /api/broadcasts/titles?q=... as the admin types. Clicking or
// keyboard-selecting a suggestion fills the input and submits the
// surrounding filter form so the table updates with one click.

const DEBOUNCE_MS = 180;
const MIN_CHARS = 1;

export function applyBroadcastsSearchTypeahead() {
  const wrap = document.querySelector(".filter-field-search");
  if (!wrap) return;
  const input = wrap.querySelector('input[name="q"]');
  if (!input) return;
  const form = input.closest("form");

  // Build the suggestion list panel — sits absolutely below the input.
  const panel = document.createElement("ul");
  panel.className = "search-suggest";
  panel.hidden = true;
  panel.setAttribute("role", "listbox");
  wrap.appendChild(panel);

  let activeIdx = -1;
  let currentResults = [];
  let debounceTimer = null;
  let lastQuery = null;
  let abortCtl = null;

  function close() {
    panel.hidden = true;
    panel.innerHTML = "";
    activeIdx = -1;
    currentResults = [];
  }

  function highlight(text, query) {
    if (!query) return text;
    const lower = text.toLowerCase();
    const q = query.toLowerCase();
    const i = lower.indexOf(q);
    if (i < 0) return escapeHtml(text);
    return (
      escapeHtml(text.slice(0, i)) +
      "<mark>" +
      escapeHtml(text.slice(i, i + query.length)) +
      "</mark>" +
      escapeHtml(text.slice(i + query.length))
    );
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function render(results, query) {
    panel.innerHTML = "";
    if (!results.length) {
      const li = document.createElement("li");
      li.className = "search-suggest-empty";
      li.textContent = "No matching broadcasts";
      panel.appendChild(li);
    } else {
      results.forEach((r, i) => {
        const li = document.createElement("li");
        li.className = "search-suggest-item";
        li.setAttribute("role", "option");
        li.dataset.idx = String(i);
        li.innerHTML =
          '<span class="search-suggest-title">' +
          highlight(r.title || "", query) +
          "</span>" +
          '<span class="search-suggest-meta">' +
          escapeHtml(r.category || "") +
          " · " +
          escapeHtml(r.delivery_channel || "") +
          "</span>";
        li.addEventListener("mousedown", (ev) => {
          // mousedown so we beat the input's blur handler.
          ev.preventDefault();
          pick(i);
        });
        li.addEventListener("mouseenter", () => setActive(i));
        panel.appendChild(li);
      });
    }
    panel.hidden = false;
    activeIdx = -1;
    currentResults = results;
  }

  function setActive(i) {
    const items = panel.querySelectorAll(".search-suggest-item");
    items.forEach((el, idx) => {
      if (idx === i) {
        el.classList.add("is-active");
        el.scrollIntoView({ block: "nearest" });
      } else {
        el.classList.remove("is-active");
      }
    });
    activeIdx = i;
  }

  function pick(i) {
    const r = currentResults[i];
    if (!r) return;
    input.value = r.title;
    close();
    if (form) form.submit();
  }

  async function fetchSuggest(query) {
    if (abortCtl) abortCtl.abort();
    abortCtl = new AbortController();
    try {
      const r = await fetch(
        "/api/broadcasts/titles?q=" + encodeURIComponent(query) + "&limit=8",
        { signal: abortCtl.signal, credentials: "same-origin" },
      );
      if (!r.ok) {
        close();
        return;
      }
      const data = await r.json();
      lastQuery = query;
      render(data || [], query);
    } catch (e) {
      if (e.name === "AbortError") return;
      close();
    }
  }

  input.addEventListener("input", () => {
    const q = input.value.trim();
    if (debounceTimer) clearTimeout(debounceTimer);
    if (q.length < MIN_CHARS) {
      close();
      return;
    }
    debounceTimer = setTimeout(() => fetchSuggest(q), DEBOUNCE_MS);
  });

  input.addEventListener("focus", () => {
    if (input.value.trim().length >= MIN_CHARS && !panel.hidden) return;
    const q = input.value.trim();
    if (q.length >= MIN_CHARS) fetchSuggest(q);
  });

  input.addEventListener("blur", () => {
    // Delay so a mousedown on a suggestion can fire first.
    setTimeout(close, 120);
  });

  input.addEventListener("keydown", (ev) => {
    if (panel.hidden) return;
    const max = currentResults.length - 1;
    if (ev.key === "ArrowDown") {
      ev.preventDefault();
      setActive(Math.min(activeIdx + 1, max));
    } else if (ev.key === "ArrowUp") {
      ev.preventDefault();
      setActive(Math.max(activeIdx - 1, 0));
    } else if (ev.key === "Enter") {
      if (activeIdx >= 0) {
        ev.preventDefault();
        pick(activeIdx);
      }
    } else if (ev.key === "Escape") {
      close();
    }
  });

  // Close when clicking outside the search field.
  document.addEventListener("mousedown", (ev) => {
    if (!wrap.contains(ev.target)) close();
  });
}