"""
har.py — Read a recorded HAR file and keep only redacted API traffic.

The HAR file stays on local disk. What leaves this module (to the learner's
model call, or as recording metadata to MongoDB) is redacted:

- Cookie, authorization and token-like values become stable placeholders:
  ``<token:1>``, ``<token:2>`` ... The same value gets the same placeholder
  everywhere, so the learner can see that the ``csrf`` in one response is the
  ``x-csrf-token`` header of the next request, without seeing the value.
- Emails become ``<email:N>`` and secrets (password, api key ...) become
  ``<secret:N>``. :meth:`Redactor.redact_text` applies the same mapping to the
  task text, so the learner still knows which typed values are params.
- Static assets, analytics hosts, ``OPTIONS`` requests and other sites are
  dropped. Large bodies are trimmed.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlsplit

MAX_STRING = 200  # characters kept per string value
MAX_LIST = 5  # items kept per JSON list
MAX_HTML_HINTS = 20
MAX_HTML_LINKS = 12

_STATIC_EXT = re.compile(
    r"\.(?:js|mjs|css|map|png|jpe?g|gif|svg|webp|ico|woff2?|ttf|otf|eot|mp4|webm|mp3)$", re.I
)
_STATIC_MIME = ("image/", "font/", "audio/", "video/", "text/css", "javascript")
_ANALYTICS_HOSTS = re.compile(
    r"(google-analytics|googletagmanager|doubleclick|segment\.io|mixpanel|hotjar|sentry\.io|"
    r"facebook\.net|clarity\.ms|plausible\.io)",
    re.I,
)
# Header and field names whose values are secrets or session tokens.
# Distinctive fragments match anywhere; short words only as whole name parts
# (so "residence" and "shipping" are not mistaken for "sid" and "pin").
_TOKEN_NAME = re.compile(
    r"(csrf|xsrf|token|nonce|signature|bearer|jwt)"
    r"|(?:^|[^a-z])(auth|authorization|session|sessionid|sid)(?:[^a-z]|$)",
    re.I,
)
_SECRET_NAME = re.compile(
    r"(password|passwd|passphrase|secret|api[_-]?key|private[_-]?key)"
    r"|(?:^|[^a-z])(otp|pin|cvv|cvc)(?:[^a-z]|$)",
    re.I,
)
_EMAIL = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}")
# Long opaque strings (ids, tokens): letters and digits mixed, 20+ chars.
_OPAQUE = re.compile(r"^(?=.*\d)(?=.*[A-Za-z])[A-Za-z0-9._~+/=-]{20,}$")
# Request headers worth showing the learner. Everything else is noise.
_KEEP_REQUEST_HEADERS = re.compile(
    r"^(content-type|accept|x-[a-z0-9-]+|origin|referer|cookie|authorization)$", re.I
)
_KEEP_RESPONSE_HEADERS = re.compile(r"^(content-type|location|set-cookie|x-[a-z0-9-]+)$", re.I)
_HTML_HINT = re.compile(
    r"<(?:input[^>]*type=[\"']?(?:hidden|radio|checkbox)[^>]*|meta[^>]*(?:csrf|xsrf|token)[^>]*"
    r"|form[^>]*|option[^>]*)>",
    re.I,
)
# href may hold HTML entities like &#39;, so '#' is allowed; bare "#..." anchors are skipped below.
_HTML_LINK = re.compile(r"<a\b[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", re.I | re.S)


class Redactor:
    """Maps sensitive values to stable placeholders for one recording."""

    def __init__(self) -> None:
        self._map: dict[str, str] = {}
        self._counts: dict[str, int] = {}

    def placeholder(self, value: str, kind: str = "token") -> str:
        if value in self._map:
            return self._map[value]
        self._counts[kind] = self._counts.get(kind, 0) + 1
        ph = f"<{kind}:{self._counts[kind]}>"
        self._map[value] = ph
        return ph

    def redact_text(self, text: str) -> str:
        """Redact free text: emails, bearer tokens, and any value already mapped."""
        for value, ph in sorted(self._map.items(), key=lambda kv: -len(kv[0])):
            if len(value) >= 4:
                text = text.replace(value, ph)
        text = _EMAIL.sub(lambda m: self.placeholder(m.group(0), "email"), text)
        return _BEARER.sub(lambda m: f"{m.group(1)} " + self.placeholder(m.group(0), "token"), text)

    def value(self, name: str, value: Any) -> Any:
        """Redact one named value (header, JSON field, form field, cookie)."""
        if isinstance(value, (dict, list)):
            return self.structure(value)
        if not isinstance(value, str):
            return value
        if not value:
            return value
        if _SECRET_NAME.search(name):
            return self.placeholder(value, "secret")
        if _TOKEN_NAME.search(name) or _OPAQUE.match(value):
            return self.placeholder(value, "token")
        text = self.redact_text(value)
        return text if len(text) <= MAX_STRING else text[:MAX_STRING] + "…"

    def structure(self, data: Any, name: str = "") -> Any:
        """Redact and trim a parsed JSON value, keeping keys and shape."""
        if isinstance(data, dict):
            return {k: self.structure(v, k) for k, v in data.items()}
        if isinstance(data, list):
            items = [self.structure(v, name) for v in data[:MAX_LIST]]
            if len(data) > MAX_LIST:
                items.append(f"… {len(data) - MAX_LIST} more")
            return items
        return self.value(name, data)


@dataclass
class Exchange:
    """One redacted request/response pair."""

    method: str
    url: str
    path: str  # path plus query, redacted
    status: int
    request_headers: dict[str, str] = field(default_factory=dict)
    request_body: Any = None
    request_format: str | None = None  # json | form | text | None
    response_headers: dict[str, str] = field(default_factory=dict)
    response_body: Any = None
    response_mime: str = ""

    def to_prompt_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"method": self.method, "url": self.url, "status": self.status}
        if self.request_headers:
            d["request_headers"] = self.request_headers
        if self.request_body is not None:
            d["request_body"] = self.request_body
            d["request_format"] = self.request_format
        if self.response_headers:
            d["response_headers"] = self.response_headers
        if self.response_body is not None:
            d["response_body"] = self.response_body
        return d

    def endpoint(self) -> dict[str, Any]:
        """Shape for ``recordings.endpoints`` (no values)."""
        return {"method": self.method, "path": urlsplit(self.url).path or "/", "status": self.status}


# -- loading -------------------------------------------------------------------


def load_har(har_path: str | Path) -> dict[str, Any]:
    return json.loads(Path(har_path).read_text(encoding="utf-8"))


def har_sha256(har_path: str | Path) -> str:
    return hashlib.sha256(Path(har_path).read_bytes()).hexdigest()


def load_exchanges(
    har_path: str | Path,
    *,
    site: str | None = None,
    redactor: Redactor | None = None,
) -> list[Exchange]:
    """Return redacted API exchanges from *har_path*, in recorded order.

    *site* (``host[:port]``) keeps only that host. Without it, every
    non-static, non-analytics host is kept. Pass your own *redactor* to reuse
    its mapping on the task text afterwards.
    """
    redactor = redactor or Redactor()
    entries = load_har(har_path).get("log", {}).get("entries", [])
    out = []
    for entry in entries:
        ex = _exchange(entry, site=site, redactor=redactor)
        if ex is not None:
            out.append(ex)
    return out


def endpoints(exchanges: Iterable[Exchange]) -> list[dict[str, Any]]:
    return [ex.endpoint() for ex in exchanges]


def _exchange(entry: dict[str, Any], *, site: str | None, redactor: Redactor) -> Exchange | None:
    req = entry.get("request", {})
    res = entry.get("response", {})
    method = str(req.get("method", "GET")).upper()
    url = str(req.get("url", ""))
    parts = urlsplit(url)
    if method in {"OPTIONS", "HEAD", "CONNECT", "TRACE"} or parts.scheme not in {"http", "https"}:
        return None
    if site and parts.netloc.lower() != site.lower():
        return None
    if _ANALYTICS_HOSTS.search(parts.netloc) or _STATIC_EXT.search(parts.path):
        return None
    mime = str(res.get("content", {}).get("mimeType", "")).lower()
    if any(m in mime for m in _STATIC_MIME):
        return None

    query = [(k, redactor.value(k, v)) for k, v in parse_qsl(parts.query, keep_blank_values=True)]
    path = parts.path or "/"
    if query:
        path += "?" + "&".join(f"{k}={v}" for k, v in query)

    req_headers = _headers(req.get("headers", []), _KEEP_REQUEST_HEADERS, redactor)
    req_body, req_format = _request_body(req.get("postData"), redactor)
    res_headers = _headers(res.get("headers", []), _KEEP_RESPONSE_HEADERS, redactor)
    res_body = _response_body(res.get("content", {}), mime, redactor)

    return Exchange(
        method=method,
        url=f"{parts.scheme}://{parts.netloc}{path}",
        path=path,
        status=int(res.get("status") or 0),
        request_headers=req_headers,
        request_body=req_body,
        request_format=req_format,
        response_headers=res_headers,
        response_body=res_body,
        response_mime=mime,
    )


def _headers(raw: list[dict[str, Any]], keep: re.Pattern[str], redactor: Redactor) -> dict[str, str]:
    out: dict[str, str] = {}
    for h in raw:
        name = str(h.get("name", "")).lower()
        if not keep.match(name) or name.startswith(":"):
            continue
        value = str(h.get("value", ""))
        if name == "cookie":
            value = "; ".join(_cookie_pair(p, redactor) for p in value.split(";") if p.strip())
        elif name == "set-cookie":
            value = _cookie_pair(value.split(";", 1)[0], redactor)
        elif name in {"authorization", "referer", "origin"}:
            value = redactor.redact_text(value) if name != "authorization" else redactor.placeholder(value)
        else:
            value = redactor.value(name, value)
        out[name] = f"{out[name]}, {value}" if name in out else value
    return out


def _cookie_pair(pair: str, redactor: Redactor) -> str:
    name, _, value = pair.strip().partition("=")
    return f"{name}={redactor.placeholder(value, 'token')}" if value else name


def _request_body(post: dict[str, Any] | None, redactor: Redactor) -> tuple[Any, str | None]:
    if not post:
        return None, None
    mime = str(post.get("mimeType", "")).lower()
    text = post.get("text") or ""
    if "json" in mime or text.lstrip().startswith(("{", "[")):
        try:
            return redactor.structure(json.loads(text)), "json"
        except ValueError:
            pass
    if "x-www-form-urlencoded" in mime:
        pairs = parse_qsl(text, keep_blank_values=True) or [
            (p.get("name", ""), p.get("value", "")) for p in post.get("params", [])
        ]
        return {k: redactor.value(k, v) for k, v in pairs}, "form"
    if "multipart" in mime:
        return {p.get("name", ""): redactor.value(p.get("name", ""), p.get("value", "")) for p in post.get("params", [])}, "form"
    return (redactor.value("body", text) if text else None), "text"


def _response_body(content: dict[str, Any], mime: str, redactor: Redactor) -> Any:
    text = content.get("text")
    if not text:
        return None
    if content.get("encoding") == "base64":
        try:
            text = base64.b64decode(text).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return None
    if "json" in mime:
        try:
            return redactor.structure(json.loads(text))
        except ValueError:
            return redactor.value("body", text)
    if "html" in mime:
        hints = [_redact_tag(tag, redactor) for tag in _HTML_HINT.findall(text)[:MAX_HTML_HINTS]]
        links = []
        for href, label in _HTML_LINK.findall(text):
            href = html.unescape(href)
            if href.startswith(("#", "mailto:", "javascript:", "http://", "https://", "//")):
                continue  # anchors and absolute links; same-site paths are enough
            label = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", label)).strip()
            links.append({"href": redactor.redact_text(href), "text": redactor.redact_text(label)[:80]})
            if len(links) >= MAX_HTML_LINKS:
                break
        out: dict[str, Any] = {}
        if hints:
            out["html_hints"] = hints
        if links:
            out["html_links"] = links
        return out or None
    return redactor.value("body", text)


def _redact_tag(tag: str, redactor: Redactor) -> str:
    """Redact the value/content attribute of a hidden input or csrf meta tag."""

    def attr(m: re.Match[str]) -> str:
        return f'{m.group(1)}="{redactor.placeholder(m.group(3), "token")}"'

    if re.search(r"(csrf|xsrf|token|hidden)", tag, re.I):
        tag = re.sub(r"\b(value|content)=([\"'])(.*?)\2", attr, tag, flags=re.I)
    return redactor.redact_text(tag)
