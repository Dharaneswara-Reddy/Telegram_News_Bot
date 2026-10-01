"""
LLM enrichment: per-item summaries, topic category and importance score,
plus the daily brief and weekly digest written over the clustered stories.

Everything here degrades gracefully. An item the LLM never gets to still
shows on the site with its feed excerpt and a heuristic category; a missing
brief just hides that panel.
"""

import json
import logging
from datetime import timedelta

from news_bot.llm import GroqClient, LLMUnavailableError
from news_bot.textutil import parse_iso, utcnow

log = logging.getLogger("news-bot.enrich")

CATEGORIES = {
    "models": "Models & products",
    "research": "Research",
    "opensource": "Open source",
    "agents": "Agents & dev tools",
    "industry": "Industry & business",
    "policy": "Policy & safety",
    "events": "Events & conferences",
    "other": "Other",
}

_SOURCE_DEFAULT = {
    "labs": "models",
    "research": "research",
    "opensource": "opensource",
    "news": "industry",
    "events": "events",
}

BATCH_SIZE = 8
MAX_ATTEMPTS = 3

ITEM_SYSTEM = """You are the editor of a personal AI news digest. The reader is a software developer who wants to keep up with everything important in AI — model and product launches, research, open source, developer tooling and agents, industry moves, and policy — without wading through hype.

For each item you receive, return:
- "summary": 1-2 plain, concrete sentences saying what actually happened or what the paper/release does. Use specific names, numbers and versions from the text. Never write "this article discusses" or marketing language. If the excerpt is empty, infer carefully from the title and say only what is clear.
- "why": one sentence on why it matters or what a developer could do with it. Use "" when there is nothing meaningful to add.
- "category": exactly one of: models (new or updated AI models and AI products/features from any company), research (papers, scientific findings, technical deep dives), opensource (open-weight models, open-source libraries, frameworks and releases), agents (coding assistants, agent frameworks, developer tools and APIs, MCP, IDE tooling), industry (funding, acquisitions, business, hardware/chips, compute, people moves), policy (regulation, law, lawsuits, safety, security, societal impact), events (conferences, workshops, competitions, deadlines), other.
- "importance": integer 1-5 for this reader. 5 = major event the whole field will talk about (a new frontier model from a top lab, a field-shifting result, a huge acquisition). 4 = notable, worth knowing this week. 3 = solid, relevant. 2 = niche or incremental. 1 = not really about AI, promotional, or trivial.
- "tags": up to 4 short lowercase tags naming the key entities or topics (e.g. "openai", "gemini-3", "reasoning", "robotics").

Reply with a JSON object: {"items": [{"id": "...", "summary": "...", "why": "...", "category": "...", "importance": 3, "tags": ["..."]}]} containing one entry for every input id."""

DAILY_SYSTEM = """You write the daily brief for a personal AI news digest read by a busy software developer. From the ranked stories of the last 24 hours, write what they must know today.

Return JSON: {"headline": "one sentence capturing the single biggest development", "bullets": [{"text": "1-2 sentences, concrete and plain", "ids": ["story id", ...]}]}
Use 4-8 bullets, most important first. Merge stories about the same event into one bullet listing all their ids. Skip trivia. Every bullet must cite at least one id from the input. No hype words, no markdown."""

WEEKLY_SYSTEM = """You write the weekly digest for a personal AI news digest read by a software developer who may not have checked in all week. From the ranked stories of the last 7 days, explain what happened and what it means.

Return JSON: {"overview": "3-4 sentence plain-language overview of the week", "sections": [{"title": "short section title", "bullets": [{"text": "1-2 sentences", "ids": ["story id", ...]}]}]}
Use 3-6 sections chosen from what actually happened (for example: Models & products, Research, Open source, Agents & tooling, Industry, Policy). 2-5 bullets per section, most important first. Every bullet must cite at least one id from the input. No hype words, no markdown."""


def default_category(item: dict) -> str:
    if item.get("kind") == "paper":
        return "research"
    if item.get("kind") in ("release", "model"):
        return "opensource"
    return _SOURCE_DEFAULT.get(item.get("src_category", ""), "other")


def _priority(item: dict, now) -> float:
    age_days = (now - parse_iso(item["published"])).total_seconds() / 86400
    meta = item.get("meta") or {}
    bonus = min(meta.get("points", 0) / 100, 3) + min(meta.get("upvotes", 0) / 20, 3)
    return item.get("weight", 3) * 2 + bonus - age_days * 1.5


def pending(items: list[dict], max_age_days: int = 14) -> list[dict]:
    """Items still needing a summary, most valuable first.

    Old items from low-weight sources are skipped outright: on a first run
    the backlog is far bigger than one day of free-tier quota, and a
    two-week-old tech-press piece isn't worth spending it on.
    """
    now = utcnow()
    out = []
    for item in items:
        if item.get("ai") or item.get("ai_attempts", 0) >= MAX_ATTEMPTS:
            continue
        age = now - parse_iso(item["published"])
        if age > timedelta(days=max_age_days) and item.get("weight", 3) < 4:
            continue
        out.append(item)
    return sorted(out, key=lambda i: _priority(i, now), reverse=True)


def _item_prompt(batch: list[dict]) -> str:
    rows = []
    for item in batch:
        row = {
            "id": item["id"],
            "source": item["source_name"],
            "title": item["title"],
            "excerpt": (item.get("excerpt") or "")[:500],
        }
        meta = item.get("meta") or {}
        if meta.get("points"):
            row["hacker_news_points"] = meta["points"]
        if meta.get("upvotes"):
            row["paper_upvotes"] = meta["upvotes"]
        rows.append(row)
    return json.dumps({"items": rows}, ensure_ascii=False)


def clean_ai(raw: dict) -> dict | None:
    summary = str(raw.get("summary") or "").strip()
    if not summary:
        return None
    category = str(raw.get("category") or "").strip().lower()
    try:
        importance = max(1, min(5, int(raw.get("importance", 3))))
    except (TypeError, ValueError):
        importance = 3
    tags = raw.get("tags") or []
    if not isinstance(tags, list):
        tags = []
    return {
        "summary": summary[:600],
        "why": str(raw.get("why") or "").strip()[:400],
        "category": category if category in CATEGORIES else "other",
        "importance": importance,
        "tags": [str(t).strip().lower()[:30] for t in tags if str(t).strip()][:4],
    }


def enrich_items(llm: GroqClient, items: list[dict], limit: int) -> list[dict]:
    """Summarise up to ``limit`` items in place. Returns the items it touched."""
    backlog = pending(items)
    queue = backlog[:limit]
    touched: list[dict] = []
    log.info("Enriching %d of %d pending items.", len(queue), len(backlog))
    for start in range(0, len(queue), BATCH_SIZE):
        batch = queue[start : start + BATCH_SIZE]
        try:
            reply = llm.chat_json(ITEM_SYSTEM, _item_prompt(batch), max_tokens=3500)
        except LLMUnavailableError as exc:
            log.warning("Stopping enrichment early: %s", exc)
            break
        by_id = {str(r.get("id")): r for r in reply.get("items", []) if isinstance(r, dict)}
        for item in batch:
            ai = clean_ai(by_id.get(item["id"], {}))
            if ai:
                item["ai"] = ai
                item.pop("ai_attempts", None)
            else:
                item["ai_attempts"] = item.get("ai_attempts", 0) + 1
            touched.append(item)
    return touched


# -- briefs -------------------------------------------------------------------


def _story_lines(stories: list[dict], limit: int) -> str:
    rows = []
    for s in stories[:limit]:
        rows.append(
            {
                "id": s["id"],
                "title": s["title"],
                "sources": [s["source_name"], *[a["source_name"] for a in s.get("also", [])][:3]],
                "summary": (s.get("summary") or s.get("excerpt") or "")[:260],
                "category": s["category"],
                "importance": s["importance"],
            }
        )
    return json.dumps({"stories": rows}, ensure_ascii=False)


def _clean_bullets(raw, valid_ids: set[str]) -> list[dict]:
    out = []
    for b in raw or []:
        if not isinstance(b, dict):
            continue
        text = str(b.get("text") or "").strip()
        ids = [i for i in (b.get("ids") or []) if i in valid_ids]
        if text and ids:
            out.append({"text": text, "ids": ids})
    return out


def daily_brief(llm: GroqClient, stories: list[dict]) -> dict | None:
    if len(stories) < 3:
        return None
    valid = {s["id"] for s in stories}
    try:
        reply = llm.chat_json(DAILY_SYSTEM, _story_lines(stories, 30), max_tokens=3000)
    except LLMUnavailableError as exc:
        log.warning("Daily brief skipped: %s", exc)
        return None
    bullets = _clean_bullets(reply.get("bullets"), valid)
    if not bullets:
        return None
    return {"headline": str(reply.get("headline") or "").strip(), "bullets": bullets}


def weekly_digest(llm: GroqClient, stories: list[dict]) -> dict | None:
    if len(stories) < 8:
        return None
    valid = {s["id"] for s in stories}
    try:
        reply = llm.chat_json(WEEKLY_SYSTEM, _story_lines(stories, 45), max_tokens=3500)
    except LLMUnavailableError as exc:
        log.warning("Weekly digest skipped: %s", exc)
        return None
    sections = []
    for sec in reply.get("sections") or []:
        if not isinstance(sec, dict):
            continue
        bullets = _clean_bullets(sec.get("bullets"), valid)
        if bullets:
            sections.append({"title": str(sec.get("title") or "").strip(), "bullets": bullets})
    if not sections:
        return None
    return {"overview": str(reply.get("overview") or "").strip(), "sections": sections}
