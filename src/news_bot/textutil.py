"""
Small, dependency-free text helpers shared by the fetchers, the store and
the clustering step: HTML→text, URL canonicalisation, stable ids, title
tokenising and timestamp handling.
"""

import hashlib
import html
import re
from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Query parameters that only track where a click came from. Dropping them
# lets the same article reached via a newsletter, HN and the lab's own feed
# collapse to one canonical URL.
_TRACKING_PARAMS = re.compile(
    r"^(utm_.*|ref|ref_src|source|fbclid|gclid|mc_cid|mc_eid|s|si|cmpid|_hsenc|_hsmi|mkt_tok)$",
    re.IGNORECASE,
)

_NOISE_FRAGMENT = re.compile(
    r"^(|comments?|respond|disqus.*|main|content|top|start|page|:~:text=.*)$", re.IGNORECASE
)

_ARXIV_ID = re.compile(r"arxiv\.org/(?:abs|pdf|html)/(\d{4}\.\d{4,5})(?:v\d+)?", re.IGNORECASE)
_HF_PAPER_ID = re.compile(r"huggingface\.co/papers/(\d{4}\.\d{4,5})", re.IGNORECASE)


def html_to_text(raw: str | None, limit: int | None = None) -> str:
    """Strip tags and entities from a feed snippet and collapse whitespace."""
    if not raw:
        return ""
    text = re.sub(r"<(script|style)\b[^<]*(?:(?!</\1>)<[^<]*)*</\1>", " ", raw, flags=re.IGNORECASE)
    text = re.sub(r"<br\s*/?>|</p>|</li>|</h\d>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t\r\f\v\xa0]+", " ", text)
    text = re.sub(r"\s*\n\s*", "\n", text).strip()
    if limit and len(text) > limit:
        cut = text[:limit].rsplit(" ", 1)[0]
        text = cut.rstrip(" ,;:.") + "…"
    return text


def canonical_url(url: str) -> str:
    """Normalise a URL so the same page from different sources compares equal.

    arXiv abstract/PDF/HTML links and Hugging Face paper pages all reduce to
    one ``arxiv:<id>`` key, since they are the same paper.
    """
    url = (url or "").strip()
    for pattern in (_ARXIV_ID, _HF_PAPER_ID):
        m = pattern.search(url)
        if m:
            return f"arxiv:{m.group(1)}"

    parts = urlsplit(url)
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query) if not _TRACKING_PARAMS.match(k)])
    path = parts.path.rstrip("/") or "/"
    # Changelog feeds (DeepSeek's, for one) give every release the same page
    # and a different #anchor, so a meaningful fragment is part of identity.
    fragment = "" if _NOISE_FRAGMENT.match(parts.fragment) else parts.fragment
    return urlunsplit(("https", host, path, query, fragment))


def short_hash(*parts: str, length: int = 12) -> str:
    return hashlib.sha1("\x1f".join(parts).encode("utf-8")).hexdigest()[:length]


_STOPWORDS = frozenset(
    """a an the and or but of for to in on at by with from into over under about as is are was
    were be been being it its this that these those you your we our they their he she his her
    how why what when where who which new now just more most than then vs via up out not no
    can will may might could should would has have had do does did get gets got make makes
    s t ll ve re d m here there all any some one two first last after before using use""".split()  # noqa: SIM905
)


def title_tokens(title: str) -> set[str]:
    """Lowercase content words of a headline, for near-duplicate detection."""
    words = re.findall(r"[a-z0-9][a-z0-9.\-+]*[a-z0-9+]|[a-z0-9]", title.lower())
    # Single digits stay: "Gemini 4" and "Gemini 3" are different stories.
    return {w for w in words if w not in _STOPWORDS and (len(w) > 1 or w.isdigit())}


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def utcnow() -> datetime:
    return datetime.now(UTC)
