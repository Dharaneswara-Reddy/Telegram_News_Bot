"""
Group items that describe the same event into a single story.

Two signals, joined with union-find:
1. The same canonical URL (an HN thread linking a lab's announcement, a
   paper on both arXiv and Hugging Face).
2. Similar headlines from different sources within a few days: either
   near-identical wording, or a shared rare name ("Argon", "World Labs")
   plus substantial overlap. Newsletter roundups are left out of this
   step — one digest headline names five launches and would chain
   unrelated stories together.

A story keeps every member as "also covered by", so the reader sees one
card per event instead of six copies of the same launch.
"""

import math
from collections import defaultdict

from news_bot.enrich import default_category
from news_bot.textutil import parse_iso, title_tokens

WINDOW_HOURS = 72
# Words appearing in more than this many headlines in the window ("openai",
# "model") are too common to be evidence that two headlines match.
MAX_TOKEN_DF = 40
# A word in at most this many headlines (scaled up for big windows) is
# treated as a distinctive name rather than a topic word.
RARE_TOKEN_DF = 10


class _UnionFind:
    def __init__(self, n: int):
        self.parent = list(range(n))

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)


def _similar(a: set[str], b: set[str], rare: set[str]) -> bool:
    common = a & b
    shared = len(common)
    if shared < 2:
        return False
    # "Gemini 3 Flash" vs "Gemini 4 Flash": different versions, different news.
    versions_a = {t for t in a if any(c.isdigit() for c in t)}
    versions_b = {t for t in b if any(c.isdigit() for c in t)}
    if versions_a and versions_b and not versions_a & versions_b:
        return False
    overlap = shared / min(len(a), len(b))
    jaccard = shared / len(a | b)
    if (shared >= 3 and overlap >= 0.75) or jaccard >= 0.6:
        return True
    return bool(common & rare) and overlap >= 0.6


def cluster_items(items: list[dict]) -> list[list[dict]]:
    items = sorted(items, key=lambda i: i["published"])
    uf = _UnionFind(len(items))

    by_key: dict[str, int] = {}
    for idx, item in enumerate(items):
        if item["key"] in by_key:
            uf.union(by_key[item["key"]], idx)
        else:
            by_key[item["key"]] = idx

    times = [parse_iso(i["published"]) for i in items]
    tokens = [
        title_tokens(i["title"])
        if i["kind"] != "paper" and i.get("src_category") != "analysis"
        else set()
        for i in items
    ]
    df: dict[str, int] = defaultdict(int)
    for toks in tokens:
        for t in toks:
            df[t] += 1
    rare_limit = max(RARE_TOKEN_DF, len(items) // 150)
    rare = {t for t, n in df.items() if 2 <= n <= rare_limit and not t.isdigit()}
    index: dict[str, list[int]] = defaultdict(list)
    for idx, toks in enumerate(tokens):
        for t in toks:
            if df[t] <= MAX_TOKEN_DF:
                index[t].append(idx)

    for idx, toks in enumerate(tokens):
        candidates: set[int] = set()
        for t in toks:
            candidates.update(j for j in index.get(t, ()) if j > idx)
        for j in candidates:
            if items[j]["source"] == items[idx]["source"]:
                continue
            if abs((times[j] - times[idx]).total_seconds()) > WINDOW_HOURS * 3600:
                continue
            if _similar(toks, tokens[j], rare):
                uf.union(idx, j)

    groups: dict[int, list[dict]] = defaultdict(list)
    for idx, item in enumerate(items):
        groups[uf.find(idx)].append(item)
    return list(groups.values())


def _default_importance(item: dict) -> int:
    return 3 if item.get("weight", 3) >= 4 else 2


def make_story(members: list[dict]) -> dict:
    members = sorted(members, key=lambda i: (-i.get("weight", 3), i["published"]))
    lead = members[0]
    enriched = [m for m in members if m.get("ai")]
    ai = (lead.get("ai") or (enriched[0]["ai"] if enriched else None)) or {}

    importance = max((m["ai"]["importance"] for m in enriched), default=_default_importance(lead))
    sources = {m["source"] for m in members}
    points = max((m["meta"].get("points", 0) for m in members), default=0)
    upvotes = max((m["meta"].get("upvotes", 0) for m in members), default=0)
    hn = next((m for m in members if m["meta"].get("hn_url")), None)
    earliest = min(members, key=lambda m: (m["published"], m["id"]))

    score = (
        importance * 10
        + lead.get("weight", 3) * 2
        + 6 * math.log2(len(sources))
        + 3 * math.log10(1 + points)
        + 3 * math.log10(1 + upvotes)
    )

    story = {
        "id": earliest["id"],
        "title": lead["title"],
        "url": lead["url"],
        "source": lead["source"],
        "source_name": lead["source_name"],
        "published": earliest["published"],
        # When the collector first saw this story; drives "new since last visit".
        "seen": min(m["fetched"] for m in members),
        "kind": "paper" if any(m["kind"] == "paper" for m in members) else lead["kind"],
        "category": ai.get("category") or default_category(lead),
        "importance": importance,
        "score": round(score, 1),
        "summary": ai.get("summary", ""),
        "why": ai.get("why", ""),
        "tags": ai.get("tags", []),
        "also": [],
    }
    if not story["summary"]:
        story["excerpt"] = (lead.get("excerpt") or "")[:400]
    if points:
        story["points"] = points
    if hn:
        story["hn_url"] = hn["meta"]["hn_url"]
        story["comments"] = hn["meta"].get("comments", 0)
    if upvotes:
        story["upvotes"] = upvotes
    for field in ("authors", "github", "org", "likes", "downloads"):
        value = next((m["meta"][field] for m in members if m["meta"].get(field)), None)
        if value:
            story[field] = value

    seen = {lead["source"]}
    for m in members[1:]:
        if m["source"] in seen:
            continue
        seen.add(m["source"])
        story["also"].append(
            {"source_name": m["source_name"], "url": m["url"], "title": m["title"]}
        )
    return story


def build_stories(items: list[dict]) -> list[dict]:
    """Cluster items into stories, dropping ones the LLM judged off-topic."""
    stories = [make_story(group) for group in cluster_items(items)]
    stories = [s for s in stories if s["importance"] > 1]
    return sorted(stories, key=lambda s: s["published"], reverse=True)
