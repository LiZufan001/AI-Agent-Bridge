"""Value-aware screening of task transport text; never return secret values.

This is a conservative publication heuristic, not a credential vault or a
proof of privacy. References and a finite set of non-secret literals are
accepted; arbitrary values are not accepted merely because they say 'test'.
"""
from __future__ import annotations

import re
from typing import Match

_KNOWN_VALUE = re.compile(
    r"(?:\bgh[pousr]_[A-Za-z0-9_]{12,}|\bgithub_pat_[A-Za-z0-9_]{12,}|"
    r"\bsk-(?:proj-)?[A-Za-z0-9_-]{16,}|"
    r"-----BEGIN (?:[A-Z0-9 ]*PRIVATE KEY|OPENSSH PRIVATE KEY)-----|"
    r"https?://[^\s/@:]+:[^\s/@]+@)", re.IGNORECASE
)
_ASSIGNMENT = re.compile(
    r"(?<![\w])(?:[A-Za-z][A-Za-z0-9]*[_-])?"
    r"(?:authorization|cookie|token|secret|password|passwd|api[_-]?key)"
    r"[\"']?\s*[:=]\s*"
    r"(?:\"(?P<double>[^\"\r\n]*)\"|'(?P<single>[^'\r\n]*)'|"
    r"(?P<bare>\$\{[A-Z_][A-Z0-9_]*\}|[^\s,;}\r\n]+))", re.IGNORECASE
)
_REFERENCE = re.compile(
    r"(?:\$\{[A-Z_][A-Z0-9_]*\}|\$[A-Z_][A-Z0-9_]*|"
    r"%[A-Z_][A-Z0-9_]*%|env:[A-Z_][A-Z0-9_]*|"
    r"<\s*(?:redacted|placeholder|secret-reference)\s*>)", re.IGNORECASE
)
_NON_SECRET_LITERALS = frozenset({
    "", "null", "none", "nil", "true", "false", "placeholder", "placeholders",
    "redacted", "<redacted>", "<placeholder>", "<secret-reference>",
    "synthetic_not_a_credential", "synthetic_not_a_secret",
})


class CredentialValuePattern:
    """Small search-compatible adapter used by existing request parsers."""

    @staticmethod
    def search(text: str) -> Match[str] | None:
        known = _KNOWN_VALUE.search(text)
        if known is not None:
            return known
        for match in _ASSIGNMENT.finditer(text):
            value = next(v for v in match.group('double', 'single', 'bare') if v is not None)
            # Strip prose punctuation, not leading prefixes or arbitrary suffixes.
            value = value.rstrip('.,;')
            if value.casefold() in _NON_SECRET_LITERALS:
                continue
            if _REFERENCE.fullmatch(value):
                continue
            return match
        return None


SECRET_VALUES = CredentialValuePattern()


def contains_secret_value(text: str) -> bool:
    return SECRET_VALUES.search(text) is not None
