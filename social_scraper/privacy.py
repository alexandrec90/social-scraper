"""What keeps personal data and secrets out of the lake.

`.claude/rules/data-lake.md` forbids personal contact data and credentials in the lake
and requires identifiers to be hashed. Two layers:

- `scrub_text` runs at ingest on every post body: e-mail addresses, phone numbers and
  @-handles / `u/` usernames are replaced with placeholders. The author is hashed
  separately (`models.AuthorInfo.hash`).
- `contains_secret` is the exporter's last gate: a row whose text still matches a known
  credential shape is **dropped**, not uploaded. Someone pasting an API key into a
  Reddit comment is exactly the payload the rule is about.
"""

from __future__ import annotations

import re

EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
# North-American shapes: (514) 555-1234, 514-555-1234, +1 514 555 1234, 514.555.1234.
PHONE_RE = re.compile(r"(?<!\w)(?:\+?1[ .-]?)?\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}(?!\w)")
X_HANDLE_RE = re.compile(r"(?<![\w@])@([A-Za-z0-9_]{1,15})\b")
REDDIT_USER_RE = re.compile(r"(?<![\w/])/?u/([A-Za-z0-9_-]{3,20})\b")

SECRET_PATTERNS = (
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),  # AWS access key id
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),  # GitHub token
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}\b"),
    re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}\b"),  # OpenAI / Anthropic keys
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),  # Slack token
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),  # Google API key
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),  # JWT
)


def scrub_text(text: str | None) -> str | None:
    """Replace contact data and user handles with placeholders."""
    if not text:
        return text
    text = EMAIL_RE.sub("[email]", text)
    text = PHONE_RE.sub("[phone]", text)
    text = X_HANDLE_RE.sub("@user", text)
    return REDDIT_USER_RE.sub("u/user", text)


def contains_secret(*texts: str | None) -> bool:
    """True if any text matches a known credential shape."""
    return any(p.search(t) for t in texts if t for p in SECRET_PATTERNS)
