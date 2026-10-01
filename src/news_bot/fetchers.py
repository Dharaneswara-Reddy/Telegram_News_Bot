"""
Fetch every source concurrently and normalise what comes back into plain
item dicts. One adapter per source kind; a failing source is reported, never
fatal, so one dead feed can't blank the whole site.
"""

import calendar
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import feedparser
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from news_bot.sources import Source
from news_bot.textutil import canonical_url, html_to_text, iso, parse_iso, short_hash, utcnow

log = logging.getLogger("news-bot.fetch")

BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/129.0.0.0 Safari/537.36"
)
REQUEST_TIMEOUT = 25
EXCERPT_CHARS = 700

# Hacker News carries everything; this keeps the AI slice. Matched against
# titles only, with word boundaries so "said" or "maid" don't count as AI.
AI_TITLE_RE = re.compile(
    r"\b(ai|a\.i\.|agi|llms?|gpt[\w.\-]*|chatgpt|openai|anthropic|claude|gemini|deepmind|"
    r"llama|mistral|qwen|deepseek|grok|xai|copilot|cursor|codex|hugging ?face|transformers?|"
    r"neural|machine learning|deep learning|ml|rlhf|diffusion|stable diffusion|midjourney|sora|"
    r"veo|inference|fine-?tun\w*|embeddings?|rag|agents?|agentic|mcp|vllm|ollama|llama\.cpp|"
    r"nvidia|gpus?|tpus?|cuda|reasoning model|language models?|foundation models?|"
    r"multimodal|chatbots?|vibe cod\w*|prompt\w*|tokens?|benchmarks?|alignment|interpretability|"
    r"robotics?|humanoid|self-driving|waymo|perplexity|kimi|moonshot|zhipu|glm|minimax)\b",
    re.IGNORECASE,
)


@dataclass
class FetchResult:
    source: Source
    items: list[dict] = field(default_factory=list)
    error: str = ""
    seconds: float = 0.0
    extra: dict = field(default_factory=dict)


def make_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=2,
        backoff_factor=1.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_maxsize=16)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def _get(session: requests.Session, url: str, source: Source, **params) -> requests.Response:
    headers = {
        "User-Agent": source.user_agent or BROWSER_UA,
        "Accept": "application/rss+xml, application/atom+xml, application/json, "
        "application/xml;q=0.9, text/xml;q=0.9, */*;q=0.8",
        "Accept-Language": "en-US,en;q=0.8",
    }
    resp = session.get(url, headers=headers, params=params or None, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp


def make_item(
    source: Source,
    *,
    url: str,
    title: str,
    published: datetime | None,
    excerpt: str = "",
    kind: str = "article",
    meta: dict | None = None,
    now: datetime | None = None,
) -> dict:
    now = now or utcnow()
    # Feeds sometimes stamp items in the future (timezone bugs) or omit the
    # date entirely; neither should float an item above genuinely new ones.
    if published is None or published > now + timedelta(hours=1):
        published = now
    key = canonical_url(url)
    return {
        "id": short_hash(source.id, key),
        "key": key,
        "source": source.id,
        "source_name": source.name,
        "src_category": source.category,
        "weight": source.weight,
        "kind": kind,
        "url": url,
        "title": re.sub(r"\s+", " ", title).strip(),
        "published": iso(published),
        "fetched": iso(now),
        "excerpt": excerpt,
        "meta": meta or {},
        "ai": None,
    }


def _entry_time(entry) -> datetime | None:
    # feedparser normalises every date it understands to a UTC struct_time.
    for attr in ("published_parsed", "updated_parsed", "created_parsed"):
        value = entry.get(attr)
        if value:
            try:
                return datetime.fromtimestamp(calendar.timegm(value), UTC)
            except (OverflowError, ValueError, TypeError):
                continue
    return None


def _entry_body(entry) -> str:
    if entry.get("summary"):
        return entry["summary"]
    content = entry.get("content") or []
    return content[0].get("value", "") if content else ""


def fetch_feed(session: requests.Session, source: Source) -> FetchResult:
    resp = _get(session, source.url, source)
    parsed = feedparser.parse(resp.content)
    if not parsed.entries and parsed.bozo:
        raise ValueError(f"not a parsable feed: {parsed.get('bozo_exception')}")

    now = utcnow()
    cutoff = now - timedelta(days=source.max_age_days)
    repo = ""
    if "github.com/" in source.url and "/releases" in source.url:
        repo = source.url.split("github.com/")[1].split("/releases")[0].split("/")[-1]
    # Some scraped feeds glue a date/category onto the headline.
    strip = re.compile(source.options["title_strip"]) if source.options.get("title_strip") else None
    items = []
    for entry in parsed.entries:
        title = html_to_text(entry.get("title", ""))
        if strip:
            title = strip.sub("", title, count=1).strip()
        link = entry.get("link") or ""
        if not title or not link.startswith("http") or not source.keeps(title):
            continue
        published = _entry_time(entry)
        if published and published < cutoff:
            continue
        # Release titles are often a bare "v0.11.0"; name the project.
        if repo and repo.lower() not in title.lower():
            title = f"{repo} {title}"
        kind = "release" if repo else "paper" if "arxiv.org" in link else "article"
        items.append(
            make_item(
                source,
                url=link,
                title=title,
                published=published,
                excerpt=html_to_text(_entry_body(entry), EXCERPT_CHARS),
                kind=kind,
                now=now,
            )
        )
        if len(items) >= source.max_items:
            break
    return FetchResult(source, items)


def fetch_hn(session: requests.Session, source: Source) -> FetchResult:
    """AI stories from Hacker News that cleared a points bar in the last ~2 days."""
    min_points = int(source.options.get("min_points", 80))
    lookback_h = int(source.options.get("lookback_hours", 48))
    since = int((utcnow() - timedelta(hours=lookback_h)).timestamp())
    resp = _get(
        session,
        source.url,
        source,
        tags="story",
        numericFilters=f"points>={min_points},created_at_i>{since}",
        hitsPerPage=300,
    )
    now = utcnow()
    items = []
    for hit in resp.json().get("hits", []):
        title = hit.get("title") or ""
        if not title or not AI_TITLE_RE.search(title) or not source.keeps(title):
            continue
        hn_url = f"https://news.ycombinator.com/item?id={hit['objectID']}"
        items.append(
            make_item(
                source,
                url=hit.get("url") or hn_url,
                title=title,
                published=parse_iso(hit.get("created_at")),
                kind="discussion",
                meta={
                    "points": hit.get("points") or 0,
                    "comments": hit.get("num_comments") or 0,
                    "hn_url": hn_url,
                },
                now=now,
            )
        )
    return FetchResult(source, items)


def fetch_hf_papers(session: requests.Session, source: Source) -> FetchResult:
    """Hugging Face Daily Papers: community-curated arXiv picks with upvotes."""
    min_upvotes = int(source.options.get("min_upvotes", 3))
    days = int(source.options.get("days", 3))
    now = utcnow()
    seen: set[str] = set()
    items = []
    for offset in range(days):
        day = (now - timedelta(days=offset)).strftime("%Y-%m-%d")
        try:
            rows = _get(session, source.url, source, date=day, limit=100).json()
        except requests.HTTPError as exc:
            # Weekend dates and "today" before the first submission can 4xx.
            if exc.response is not None and exc.response.status_code in (400, 404):
                continue
            raise
        rows = sorted(rows, key=lambda r: (r.get("paper") or {}).get("upvotes", 0), reverse=True)
        for row in rows[: source.max_items]:
            paper = row.get("paper") or {}
            pid = paper.get("id")
            upvotes = paper.get("upvotes", 0)
            if not pid or pid in seen or upvotes < min_upvotes:
                continue
            seen.add(pid)
            authors = [a.get("name") for a in paper.get("authors", []) if a.get("name")]
            meta = {"upvotes": upvotes, "comments": row.get("numComments", 0)}
            if authors:
                meta["authors"] = authors[:6]
            if paper.get("githubRepo"):
                meta["github"] = paper["githubRepo"]
            org = (paper.get("organization") or row.get("organization") or {}).get("fullname")
            if org:
                meta["org"] = org
            items.append(
                make_item(
                    source,
                    url=f"https://huggingface.co/papers/{pid}",
                    title=paper.get("title") or row.get("title") or pid,
                    published=parse_iso(paper.get("submittedOnDailyAt"))
                    or parse_iso(row.get("publishedAt")),
                    excerpt=html_to_text(paper.get("summary"), EXCERPT_CHARS),
                    kind="paper",
                    meta=meta,
                    now=now,
                )
            )
    return FetchResult(source, items)


def fetch_hf_trending(session: requests.Session, source: Source) -> FetchResult:
    """Trending Hugging Face models.

    The full ranked list is returned as ``extra`` for the Open Source page.
    Recently created models that break into the top of the list also become
    news items, which is how most open-weight releases first surface.
    """
    min_downloads = int(source.options.get("min_downloads", 500))
    new_within = timedelta(days=int(source.options.get("new_within_days", 21)))
    top_n = int(source.options.get("top_n", 20))
    rows = _get(session, source.url, source, sort="trendingScore", direction=-1, limit=60).json()
    now = utcnow()
    trending, items = [], []
    for row in rows:
        # Likes are cheap to game; a model nobody downloads isn't trending.
        if (row.get("downloads") or 0) < min_downloads:
            continue
        model_id = row.get("id") or row.get("modelId")
        created = parse_iso(row.get("createdAt"))
        entry = {
            "id": model_id,
            "url": f"https://huggingface.co/{model_id}",
            "likes": row.get("likes", 0),
            "downloads": row.get("downloads", 0),
            "task": row.get("pipeline_tag") or "",
            "created": iso(created) if created else None,
        }
        trending.append(entry)
        if len(trending) <= top_n and created and now - created <= new_within:
            items.append(
                make_item(
                    source,
                    url=entry["url"],
                    title=f"{model_id} is trending on Hugging Face",
                    published=created,
                    excerpt=(
                        f"{entry['task'] or 'model'} · {entry['likes']:,} likes · "
                        f"{entry['downloads']:,} downloads"
                    ),
                    kind="model",
                    meta={"likes": entry["likes"], "downloads": entry["downloads"]},
                    now=now,
                )
            )
    return FetchResult(source, items, extra={"trending_models": trending[:30]})


ADAPTERS = {
    "feed": fetch_feed,
    "hn": fetch_hn,
    "hf_papers": fetch_hf_papers,
    "hf_trending": fetch_hf_trending,
}


def _run_one(session: requests.Session, source: Source) -> FetchResult:
    started = time.monotonic()
    try:
        result = ADAPTERS[source.kind](session, source)
    except Exception as exc:
        detail = str(exc)
        if isinstance(exc, requests.HTTPError) and exc.response is not None:
            detail = f"HTTP {exc.response.status_code}"
        log.warning("Fetch failed for %s: %s", source.id, detail[:200])
        result = FetchResult(source, error=detail[:300])
    result.seconds = round(time.monotonic() - started, 2)
    return result


def fetch_all(sources: list[Source], workers: int = 12) -> list[FetchResult]:
    session = make_session()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(lambda s: _run_one(session, s), sources))
    ok = sum(1 for r in results if not r.error)
    total = sum(len(r.items) for r in results)
    log.info("Fetched %d/%d sources OK, %d items.", ok, len(results), total)
    return results
