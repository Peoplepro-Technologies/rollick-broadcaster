"""Unit tests for scripts.tunnel_kv.

The bash script scripts/start-tunnel.sh delegates the testable parts
(URL construction, KV PUT/GET, health probe) to this module so we can
verify the round-trip behaviour without touching the real Cloudflare
API. All HTTP is mocked at the urllib.request.urlopen seam.
"""
from __future__ import annotations

import io
import json
import urllib.error
from contextlib import contextmanager
from unittest.mock import patch

import pytest

from scripts import tunnel_kv


# ---- helpers ----------------------------------------------------------------


def _ok(status: int, body: str = ""):
    """Build a fake context-manager response from urlopen."""
    resp = io.BytesIO(body.encode("utf-8"))
    resp.status = status
    return resp


def _http_error(status: int, body: str = ""):
    """Build a real HTTPError that urlopen would raise on non-2xx."""
    return urllib.error.HTTPError(
        "https://api.example/values/current",
        status,
        "Error",
        {},
        io.BytesIO(body.encode("utf-8")),
    )


def _urlopen_seq(responses):
    """Return a urlopen-replacement that yields the given responses in order.

    A response may be either a (status, body) tuple OR an Exception instance
    (e.g. HTTPError) to be raised — mirrors what urlopen does on non-2xx.
    """
    it = iter(responses)

    def fake(req, *args, **kwargs):
        # Accept both Request object and plain url string; urlopen in
        # health_probe gets the latter.
        item = next(it)
        if isinstance(item, Exception):
            raise item
        status, body = item
        resp = io.BytesIO(body.encode("utf-8"))
        resp.status = status
        return resp

    return fake


# ---- build_kv_url -----------------------------------------------------------


def test_build_kv_url_default_key():
    url = tunnel_kv.build_kv_url("acct123", "ns456")
    assert (
        url
        == "https://api.cloudflare.com/client/v4/accounts/acct123/storage/kv/namespaces/ns456/values/current"
    )


def test_build_kv_url_custom_key():
    url = tunnel_kv.build_kv_url("a", "n", key="backend")
    assert url.endswith("/values/backend")


# ---- kv_put -----------------------------------------------------------------


def test_kv_put_happy_path():
    with patch("urllib.request.urlopen", _urlopen_seq([(200, '{"success": true}')])):
        tunnel_kv.kv_put("https://x", "https://abc.trycloudflare.com", "tok")


def test_kv_put_accepts_201_too():
    with patch("urllib.request.urlopen", _urlopen_seq([(201, '{"success": true}')])):
        tunnel_kv.kv_put("https://x", "v", "tok")


def test_kv_put_http_error_includes_body():
    err = _http_error(401, '{"success":false,"errors":[{"message":"bad token"}]}')
    with patch("urllib.request.urlopen", _urlopen_seq([err])):
        with pytest.raises(RuntimeError, match=r"KV PUT failed: HTTP 401"):
            tunnel_kv.kv_put("https://x", "v", "bad")


def test_kv_put_success_false_raises():
    with patch(
        "urllib.request.urlopen",
        _urlopen_seq([(200, '{"success": false, "errors": [{"message": "denied"}]}')]),
    ):
        with pytest.raises(RuntimeError, match=r"KV PUT failed"):
            tunnel_kv.kv_put("https://x", "v", "tok")


def test_kv_put_non_json_response_raises():
    with patch("urllib.request.urlopen", _urlopen_seq([(200, "<html>not json</html>")])):
        with pytest.raises(RuntimeError, match=r"non-JSON"):
            tunnel_kv.kv_put("https://x", "v", "tok")


# ---- kv_get -----------------------------------------------------------------


def test_kv_get_returns_value():
    with patch(
        "urllib.request.urlopen",
        _urlopen_seq([(200, "https://abc.trycloudflare.com")]),
    ):
        assert tunnel_kv.kv_get("https://x", "tok") == "https://abc.trycloudflare.com"


def test_kv_get_http_error_raises():
    err = _http_error(403, '{"success":false}')
    with patch("urllib.request.urlopen", _urlopen_seq([err])):
        with pytest.raises(RuntimeError, match=r"KV GET failed: HTTP 403"):
            tunnel_kv.kv_get("https://x", "tok")


# ---- health_probe -----------------------------------------------------------


def test_health_probe_appends_api_health_path():
    seen = []

    def fake(url, *a, **kw):
        seen.append(url)
        return _ok(200)

    with patch("urllib.request.urlopen", fake):
        tunnel_kv.health_probe("https://abc.workers.dev")
    assert seen == ["https://abc.workers.dev/api/health"]


def test_health_probe_strips_trailing_slash():
    seen = []

    def fake(url, *a, **kw):
        seen.append(url)
        return _ok(200)

    with patch("urllib.request.urlopen", fake):
        tunnel_kv.health_probe("https://abc.workers.dev/")
    assert seen == ["https://abc.workers.dev/api/health"]


def test_health_probe_returns_200_on_success():
    with patch("urllib.request.urlopen", _urlopen_seq([(200, "")])):
        assert tunnel_kv.health_probe("https://abc.workers.dev") == 200


def test_health_probe_returns_status_on_5xx():
    """If the final response (after redirects) is non-2xx, return that code."""
    err = _http_error(502, "Bad Gateway")
    with patch("urllib.request.urlopen", _urlopen_seq([err])):
        assert tunnel_kv.health_probe("https://abc.workers.dev") == 502


# ---- main CLI ---------------------------------------------------------------


def test_main_put_delegates(monkeypatch):
    captured = {}

    def fake_put(url, value, token, **kw):
        captured["url"] = url
        captured["value"] = value
        captured["token"] = token

    monkeypatch.setattr(tunnel_kv, "kv_put", fake_put)
    rc = tunnel_kv.main(
        ["put", "https://abc.trycloudflare.com", "--account", "acct", "--ns", "ns", "--token", "tok"]
    )
    assert rc == 0
    assert captured["value"] == "https://abc.trycloudflare.com"
    assert captured["token"] == "tok"
    assert captured["url"].endswith("/values/current")


def test_main_get_prints_value(monkeypatch, capsys):
    monkeypatch.setattr(tunnel_kv, "kv_get", lambda *a, **kw: "https://abc.trycloudflare.com")
    rc = tunnel_kv.main(["get", "--account", "a", "--ns", "n", "--token", "t"])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "https://abc.trycloudflare.com"


def test_main_probe_returns_0_on_200(monkeypatch, capsys):
    monkeypatch.setattr(tunnel_kv, "health_probe", lambda *a, **kw: 200)
    rc = tunnel_kv.main(["probe", "https://abc.workers.dev"])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "200"


def test_main_probe_returns_1_on_non_200(monkeypatch, capsys):
    monkeypatch.setattr(tunnel_kv, "health_probe", lambda *a, **kw: 502)
    rc = tunnel_kv.main(["probe", "https://abc.workers.dev"])
    assert rc == 1
    assert capsys.readouterr().out.strip() == "502"


def test_main_runtime_error_writes_to_stderr(monkeypatch, capsys):
    def boom(*a, **kw):
        raise RuntimeError("network down")

    monkeypatch.setattr(tunnel_kv, "kv_put", boom)
    rc = tunnel_kv.main(["put", "v", "--account", "a", "--ns", "n", "--token", "t"])
    assert rc == 1
    assert "network down" in capsys.readouterr().err
