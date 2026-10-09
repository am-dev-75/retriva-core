# Copyright (C) 2026 Andrea Marson (am.dev.75@gmail.com)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Fail-closed URL redaction for logs, stdout and diagnostics.

Spec 034 §7 / acceptance B8 and Constitution §34 require that credentials
never appear in logs, exception messages or artifacts.  Service URLs
(broker, result backend, Qdrant, embedding providers, AMQP, databases)
may carry userinfo; this module renders them without any userinfo and
without secret-bearing query values.

Display policy (single, consistent):

    scheme://host:port/path?key=***

* All userinfo (username *and* password) is dropped: ``redis://redis:6379/0``.
* Query-string values are replaced by ``***``; key names are kept because
  they are operationally useful and not secret by convention.
* Fragments are dropped.
* Parsing failures (malformed URL-like input, out-of-range ports,
  invalid IPv6 brackets, ...) fail closed by returning a fixed marker;
  the original value is never echoed and no exception is raised.
* Repeated application is stable (idempotent).
"""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import SplitResult, urlsplit, urlunsplit

# Fixed, value-free marker for input that cannot be parsed safely.
REDACTED_URL_MARKER = "[redacted-url]"

# scheme://userinfo@... — matches URL-like substrings inside free text.
_URL_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://[^\s<>\"'\)\]]+")
# Trailing punctuation that commonly delimits a URL inside prose/punctuation.
_TRAILING_PUNCTUATION = ",.;:!?'\")]"


def _redact_query(query: str) -> str:
    """Keep query key names, replace all values with the masked token."""
    if not query:
        return ""
    parts = []
    for part in query.split("&"):
        if "=" in part:
            key = part.split("=", 1)[0]
            parts.append(f"{key}=***")
        else:
            # Bare part: keep only the name, mask the (empty) value position.
            parts.append(f"{part}=***")
    return "&".join(parts)


def _rebuild(parts: SplitResult, *, drop_scheme: bool = False) -> str:
    """Rebuild a split URL without userinfo, fragment or query values."""
    host = parts.hostname or ""
    if ":" in host:  # IPv6 literal
        host = f"[{host}]"
    netloc = host
    try:
        port = parts.port
    except ValueError as exc:  # out-of-range or malformed port
        raise ValueError("malformed port") from exc
    if port is not None:
        netloc = f"{netloc}:{port}"
    scheme = "" if drop_scheme else parts.scheme
    query = _redact_query(parts.query)
    if not netloc and parts.path.startswith("/"):
        # Preserve authority-less forms such as redis+socket:///run/redis.sock
        # (urlunsplit would collapse the third slash).
        base = f"{scheme}://{parts.path}"
    else:
        base = urlunsplit((scheme, netloc, parts.path, "", ""))
    return f"{base}?{query}" if query else base


def redact_url(value: Optional[str]) -> str:
    """Return *value* with any credential-bearing userinfo removed.

    The function never raises and never includes the original value in its
    output for malformed input: it fails closed to ``REDACTED_URL_MARKER``.
    ``None`` is rendered as an empty string.
    """
    if value is None:
        return ""
    text = str(value)
    if not text:
        return text
    try:
        if "://" in text:
            return _rebuild(urlsplit(text))
        # Scheme-less URL-like userinfo, e.g. "user:pass@host:6379/0".
        if "@" in text:
            head = text.partition("@")[0]
            if (":" in head) and ("/" not in head):
                return _rebuild(urlsplit("//" + text), drop_scheme=True)
        # Not URL-like: nothing that can legally be a URL secret.
        return text
    except Exception:
        # Fail closed: never echo a value that could not be parsed safely.
        return REDACTED_URL_MARKER


def redact_urls_in_text(text: Optional[str]) -> str:
    """Redact every URL-like substring in *text* (exception messages, log lines).

    Python regular-expression based; unlike :func:`redact_url` this helper is
    for free-form text and therefore never fails closed over the whole string —
    non-URL text is preserved verbatim.  Any URL-like match is processed by
    :func:`redact_url`, including trailing-punctuation handling.
    """
    if text is None:
        return ""
    value = str(text)

    def _replace(match: re.Match) -> str:
        candidate = match.group(0)
        trailing = ""
        while candidate and candidate[-1] in _TRAILING_PUNCTUATION:
            trailing = candidate[-1] + trailing
            candidate = candidate[:-1]
        return redact_url(candidate) + trailing

    try:
        return _URL_RE.sub(_replace, value)
    except Exception:
        # Fail closed without echoing the original text.
        return REDACTED_URL_MARKER
