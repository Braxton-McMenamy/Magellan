"""The website's security headers, in one place: ``magellan-lite serve`` sends them, and
``demo/build_site.py`` writes them to ``site/_headers``, which Cloudflare Pages applies to
every file it serves. Whichever host answers, a page may load and contact exactly this:

- scripts: the site's own, the one inline script (by hash), and Pyodide from the CDN, which
  runs the engine in the browser (``'wasm-unsafe-eval'`` lets it compile WebAssembly; nothing
  allows ``eval`` of text);
- connections: the site itself, the CDN (Pyodide's packages), and GitHub's API and raw files,
  which the Team suite reads a repository's shared work in progress from;
- no frames, no plugins, no forms posting anywhere, no ``<base>`` tricks.
"""

from __future__ import annotations

import base64
import hashlib
import re
from pathlib import Path

PYODIDE_CDN = "https://cdn.jsdelivr.net"
GITHUB = ("https://api.github.com", "https://raw.githubusercontent.com")


def inline_script_hashes(site: Path) -> list[str]:
    """CSP hashes for the inline scripts of the site's pages, so nothing else inline runs."""
    out = set()
    for page in site.glob("*.html"):
        for body in re.findall(r"<script>(.*?)</script>", page.read_text("utf-8", "replace"), re.S):
            out.add("'sha256-" + base64.b64encode(hashlib.sha256(body.encode()).digest()).decode() + "'")
    return sorted(out)


def csp(site: Path) -> str:
    return "; ".join([
        "default-src 'self'",
        " ".join(["script-src 'self'", *inline_script_hashes(site), PYODIDE_CDN,
                  "'wasm-unsafe-eval'"]),
        "worker-src 'self'",
        "style-src 'self' 'unsafe-inline'",           # style="--heat: .8" on map nodes
        "img-src 'self' data:",
        " ".join(["connect-src 'self'", PYODIDE_CDN, *GITHUB]),
        "object-src 'none'",
        "base-uri 'none'",
        "frame-ancestors 'none'",
        "form-action 'none'",
    ])


def headers(site: Path) -> dict[str, str]:
    """Every security header, for every response."""
    return {
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "no-referrer",
        "Cross-Origin-Opener-Policy": "same-origin",
        "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
        "Strict-Transport-Security": "max-age=31536000",
        "Content-Security-Policy": csp(site),
    }


def pages_headers(site: Path) -> str:
    """``site/_headers`` for Cloudflare Pages: the same headers on every path."""
    lines = ["# Security headers for Cloudflare Pages: written by demo/build_site.py from",
             "# magellan_lite/security.py -- do not edit by hand", "/*"]
    lines += [f"  {k}: {v}" for k, v in headers(site).items()]
    return "\n".join(lines) + "\n"
