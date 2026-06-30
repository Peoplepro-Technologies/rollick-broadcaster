"""Cloudflare KV operations for the start-tunnel script.

The bash script scripts/start-tunnel.sh delegates the testable parts
(URL construction, KV PUT/GET, health probe) to this module so we can
verify the round-trip behaviour without touching the real Cloudflare
API. All HTTP is routed through small functions (build_kv_url, kv_put,
kv_get, health_probe) so tests can mock them at the
urllib.request.urlopen seam.

CLI usage (called from the bash script):

    python -m scripts.tunnel_kv put  <value> --account A --ns N --token T
    python -m scripts.tunnel_kv get            --account A --ns N --token T
    python -m scripts.tunnel_kv probe <public_url>

Exits 0 on success, 1 on any failure (with the error message on stderr).
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from typing import Optional


# ---- pure helpers ----------------------------------------------------------


def build_kv_url(account_id: str, namespace_id: str, key: str = "current") -> str:
    """Return the Cloudflare API URL for a KV value."""
    return (
        f"https://api.cloudflare.com/client/v4/accounts/"
        f"{account_id}/storage/kv/namespaces/{namespace_id}/values/{key}"
    )


# ---- Cloudflare KV HTTP -----------------------------------------------------


def kv_put(url: str, value: str, token: str, *, timeout: float = 10.0) -> None:
    """PUT `value` to the Cloudflare KV API. Raises RuntimeError on failure."""
    req = urllib.request.Request(
        url,
        data=value.encode("utf-8"),
        method="PUT",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "text/plain",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            body_text = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"KV PUT failed: HTTP {e.code} {body_text[:200]}"
        ) from e
    if status not in (200, 201):
        raise RuntimeError(f"KV PUT failed: HTTP {status} {body_text[:200]}")
    try:
        body = json.loads(body_text)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"KV PUT returned non-JSON: {body_text[:200]}") from e
    if not body.get("success"):
        raise RuntimeError(f"KV PUT failed: {body}")


def kv_get(url: str, token: str, *, timeout: float = 10.0) -> str:
    """GET a value from the Cloudflare KV API. Raises RuntimeError on failure."""
    req = urllib.request.Request(
        url,
        method="GET",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"KV GET failed: HTTP {e.code} {body_text[:200]}"
        ) from e


# ---- public URL health probe ------------------------------------------------


def health_probe(public_url: str, *, timeout: float = 10.0) -> int:
    """Hit <public_url>/api/health and return the final HTTP status.

    urllib follows 307 redirects automatically (Python 3), so the Worker
    307 → trycloudflare backend → 200 chain is transparent. A non-2xx
    final response is returned as-is (so the caller can branch on 502/503).
    """
    health_url = public_url.rstrip("/") + "/api/health"
    try:
        with urllib.request.urlopen(health_url, timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        # urlopen raises on non-2xx AFTER following any redirects, so this
        # is the terminal code the user actually reached.
        return e.code
    except urllib.error.URLError as e:
        raise RuntimeError(f"Health probe failed: {e.reason}") from e


# ---- CLI entry point --------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Cloudflare KV operations for the start-tunnel script.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_put = sub.add_parser("put", help="PUT a value into KV")
    p_put.add_argument("value")
    p_put.add_argument("--account", required=True)
    p_put.add_argument("--ns", required=True)
    p_put.add_argument("--token", required=True)
    p_put.add_argument("--key", default="current")

    p_get = sub.add_parser("get", help="GET a value from KV")
    p_get.add_argument("--account", required=True)
    p_get.add_argument("--ns", required=True)
    p_get.add_argument("--token", required=True)
    p_get.add_argument("--key", default="current")

    p_probe = sub.add_parser("probe", help="Probe <url>/api/health, follow redirects")
    p_probe.add_argument("url")

    args = parser.parse_args(argv)

    try:
        if args.cmd == "put":
            url = build_kv_url(args.account, args.ns, args.key)
            kv_put(url, args.value, args.token)
        elif args.cmd == "get":
            url = build_kv_url(args.account, args.ns, args.key)
            print(kv_get(url, args.token))
        elif args.cmd == "probe":
            code = health_probe(args.url)
            print(code)
            if code != 200:
                return 1
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
