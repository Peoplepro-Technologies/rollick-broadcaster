"""Static asset reference test — defense in depth.

Jinja templates and base.html style fragments reference static files
via absolute paths like `/static/js/foo.js`, `/static/css/bar.css`,
or `/static/img/logo.png`. The FastAPI app only mounts ONE static
directory (BASE_DIR / "static"); anything under
`broadcaster/static/` is invisible to the client.

Regression caught: `broadcaster/static/js/broadcasts.js` was imported
from `broadcaster/templates/admin/broadcasts_list.html` but never
copied to the served `static/` dir. The browser got 404 on the import,
which aborted the entire ES module — including the schedule.js
formatter — so the broadcast list's "scheduled time" cells stayed as
`…` placeholders forever.

This test fails fast at CI time, before the user sees the bug.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = ROOT / "broadcaster" / "templates"
SERVED_STATIC_DIR = ROOT / "static"

# Templates deliberately serve third-party CSS (Google Fonts) over CDN —
# filter for our own /static/ path only.
STATIC_REF_RE = re.compile(r'(?<!//)(?<!:)"/static/([^"]+)"')


def _collect_template_paths() -> list[Path]:
    return sorted(TEMPLATES_DIR.rglob("*.html"))


def _extract_static_refs(template_text: str) -> list[str]:
    return [m.group(1).split("?", 1)[0]  # strip ?v=12 cache-busters
            for m in STATIC_REF_RE.finditer(template_text)]


@pytest.mark.parametrize("tmpl", _collect_template_paths(),
                         ids=lambda p: str(p.relative_to(ROOT)))
def test_static_references_resolve_on_disk(tmpl: Path) -> None:
    """Every `/static/...` reference in a template must point at a real
    file under the served `static/` dir.

    Templates that inherit *all* their static refs via `{% extends
    "base.html" %}` legitimately have zero of their own — skip those.
    """
    text = tmpl.read_text(encoding="utf-8")
    refs = _extract_static_refs(text)
    if not refs:
        # Inherits from base.html (or another template that owns the refs).
        # The base template's own parametrized case covers it.
        pytest.skip(f"{tmpl.relative_to(ROOT)} inherits its static refs")
    missing = [r for r in refs if not (SERVED_STATIC_DIR / r).is_file()]
    assert not missing, (
        f"{tmpl.relative_to(ROOT)} references files that don't exist "
        f"under {SERVED_STATIC_DIR}: {missing}. "
        f"Either add the file to static/ (the served dir) or fix the path."
    )


def test_broadcasts_list_uses_broadcasts_js_for_typeahead() -> None:
    """Regression: broadcasts_list.html must import broadcasts.js from the
    served /static path (not the blueprint-only path). The typeahead works
    alongside schedule.js's list formatter — losing either kills the
    schedule display."""
    text = (TEMPLATES_DIR / "admin" / "broadcasts_list.html").read_text()
    assert '"/static/js/broadcasts.js"' in text, (
        "broadcasts_list.html should import the typeahead module from the "
        "served /static/ path."
    )
    assert '"/static/js/schedule.js"' in text, (
        "broadcasts_list.html should import schedule.js (applyListFormatter) "
        "from the served /static/ path."
    )
