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

"""Deterministic tests for the fail-closed URL redaction helper.

Spec 034 §7 / acceptance B8 / Constitution §34: credentials must never
appear in logs, exception messages or artifacts.  These tests assert
against the exact synthetic secrets and their encoded forms, not only
against URL-shaped regular expressions.
"""

from urllib.parse import quote

import pytest

from retriva.logger.redaction import (
    REDACTED_URL_MARKER,
    redact_url,
    redact_urls_in_text,
)

# Plain synthetic secret for structural (exact-output) assertions.
SECRET_PLAIN = "S3cr3tToken123"

# Reserved-character / Unicode secrets for absence and fail-closed assertions.
SECRET = "S3cr3t-P@ss/word:With?Reserved#Chars"
SECRET_UNICODE = "антoppa-秘密"
SECRET_ENCODED = "p%40ss%2Fword%3A%3F%23"


def _assert_no_secret(text: str, secret: str = SECRET) -> None:
    assert secret not in text, text
    assert quote(secret, safe="") not in text, text
    assert secret.replace("@", "%40") not in text, text
    assert secret.replace("@", "%40").replace(":", "%3A") not in text, text
    assert SECRET_ENCODED not in text, text


@pytest.mark.parametrize(
    "value,expected",
    [
        ("redis://rtrv-broker:" + SECRET_PLAIN + "@redis:6379/0",
         "redis://redis:6379/0"),
        ("redis://:" + SECRET_PLAIN + "@redis:6379/0",
         "redis://redis:6379/0"),
        ("redis://user@redis:6379/0", "redis://redis:6379/0"),
        ("redis://user:pass@10.0.0.5:6379/0", "redis://10.0.0.5:6379/0"),
        ("redis://user:pass@[2001:db8::1]:6379/0",
         "redis://[2001:db8::1]:6379/0"),
        ("redis://redis:6379/0", "redis://redis:6379/0"),
        ("redis+socket:///run/redis.sock", "redis+socket:///run/redis.sock"),
        ("amqps://u:p@broker.example:5671/vhost",
         "amqps://broker.example:5671/vhost"),
        ("http://u:p@[::1]:8000/path", "http://[::1]:8000/path"),
        ("postgresql://rtrv_core:pw@postgres:5432/retriva",
         "postgresql://postgres:5432/retriva"),
    ],
)
def test_redact_well_formed(value, expected):
    result = redact_url(value)
    assert result == expected
    assert "rtrv_" not in result
    assert ":pw@" not in result


def test_redact_password_only_and_username_only():
    assert redact_url("redis://:" + SECRET_PLAIN + "@h:1/0") == "redis://h:1/0"
    assert redact_url("redis://u@" + "h:1/0") == "redis://h:1/0"


def test_redact_percent_encoded_userinfo():
    value = "redis://us%40er:" + SECRET_ENCODED + "@redis:6379/0"
    result = redact_url(value)
    assert result == "redis://redis:6379/0"
    assert SECRET_ENCODED not in result


def test_redact_reserved_chars_in_userinfo_are_dropped():
    # Percent-encoded reserved characters inside userinfo are dropped with the
    # whole userinfo segment.
    value = "redis://user:S3cr3t%2DP%40ss@redis:6379/0"
    result = redact_url(value)
    assert result == "redis://redis:6379/0"
    assert "S3cr3t" not in result
    assert "P%40ss" not in result


def test_redact_unicode_userinfo():
    value = "redis://user:" + SECRET_UNICODE + "@redis:6379/0"
    result = redact_url(value)
    assert result == "redis://redis:6379/0"
    assert SECRET_UNICODE not in result


def test_redact_query_values_masked_keys_kept():
    value = "redis://u:" + SECRET_PLAIN + "@redis:6379/0?db=0&password=" + SECRET_PLAIN + "&flag"
    result = redact_url(value)
    assert result == "redis://redis:6379/0?db=***&password=***&flag=***"
    assert SECRET_PLAIN not in result


def test_redact_fragment_dropped():
    result = redact_url("redis://u:p@redis:6379/0#fragment-" + SECRET_PLAIN)
    assert result == "redis://redis:6379/0"
    assert "fragment" not in result


def test_redact_scheme_less_userinfo():
    result = redact_url("user:" + SECRET_PLAIN + "@redis:6379/0")
    assert result == "//redis:6379/0"
    assert SECRET_PLAIN not in result


@pytest.mark.parametrize(
    "malformed",
    [
        "redis://user:x@[::1",                     # unclosed IPv6 bracket
        "redis://user:pass@host:99999/0",          # out-of-range port
        "redis://user:p#ss@host:6379/0",           # '#' splits netloc -> bad port
        "redis://user:pa?ss@host:6379/0",          # '?' splits netloc -> bad port
        "redis://user:s3cr3t-without-host",        # ':' port = non-numeric
    ],
)
def test_redact_malformed_fails_closed(malformed):
    result = redact_url(malformed)
    assert result == REDACTED_URL_MARKER
    assert "s3cr3t" not in result
    assert "pass" not in result


def test_redact_ambiguous_userinfo_never_echoes_secret():
    # Userinfo containing '/' is structurally ambiguous; whatever the parse,
    # the secret-bearing userinfo segment must not survive.
    result = redact_url("redis://user:" + SECRET)
    _assert_no_secret(result)


def test_redact_empty_and_none():
    assert redact_url("") == ""
    assert redact_url(None) == ""


def test_redact_stability():
    for value in (
        "redis://u:" + SECRET_PLAIN + "@redis:6379/0?db=0",
        "redis://redis:6379/0",
        "not-a-url",
        REDACTED_URL_MARKER,
        "redis://user:pass@[::1",
        "redis://user:" + SECRET,
    ):
        once = redact_url(value)
        twice = redact_url(once)
        assert once == twice


def test_redact_non_url_text_untouched():
    assert redact_url("(same as broker)") == "(same as broker)"
    assert redact_url("redis") == "redis"


def test_redact_urls_in_text_redacts_exception_style_lines():
    line = ("Cannot connect to redis://rtrv-broker:" + SECRET_PLAIN
            + "@redis:6379/0: refused.")
    result = redact_urls_in_text(line)
    assert "redis://redis:6379/0: refused." in result
    assert SECRET_PLAIN not in result


def test_redact_urls_in_text_plain_text_untouched():
    assert redact_urls_in_text("connection refused") == "connection refused"
    assert redact_urls_in_text(None) == ""
    assert redact_urls_in_text("") == ""
