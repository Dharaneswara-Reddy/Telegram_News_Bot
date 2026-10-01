"""
Source registry. The list itself lives in ``sources.toml`` next to this
file so adding or muting a feed is a one-line data change, not a code edit.
"""

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

SOURCES_FILE = Path(__file__).with_name("sources.toml")

# Feed categories describe where a source sits, not what a given story is
# about — the LLM assigns each story its own topic category later.
SOURCE_CATEGORIES = ("labs", "research", "opensource", "analysis", "news", "community", "events")

# "feed" covers RSS and Atom (feedparser handles both); the rest are JSON
# APIs with their own adapters in fetchers.py.
KINDS = ("feed", "hn", "hf_papers", "hf_trending")


@dataclass(frozen=True)
class Source:
    id: str
    name: str
    url: str
    category: str
    weight: int = 3
    kind: str = "feed"
    homepage: str = ""
    # Optional regexes applied to titles, for mixed feeds (keep only AI
    # stories) and noisy release feeds (drop nightly build tags).
    include: str = ""
    exclude: str = ""
    max_items: int = 40
    max_age_days: int = 21
    user_agent: str = ""
    enabled: bool = True
    options: dict = field(default_factory=dict)

    def keeps(self, title: str) -> bool:
        if self.include and not re.search(self.include, title, re.IGNORECASE):
            return False
        return not (self.exclude and re.search(self.exclude, title, re.IGNORECASE))


def load_sources(path: Path = SOURCES_FILE, include_disabled: bool = False) -> list[Source]:
    with path.open("rb") as fh:
        raw = tomllib.load(fh)

    known = set(Source.__dataclass_fields__) - {"options"}
    sources: list[Source] = []
    seen: set[str] = set()
    for entry in raw.get("source", []):
        extra = {k: v for k, v in entry.items() if k not in known}
        src = Source(**{k: v for k, v in entry.items() if k in known}, options=extra)
        if src.category not in SOURCE_CATEGORIES:
            raise ValueError(f"{src.id}: unknown category {src.category!r}")
        if src.kind not in KINDS:
            raise ValueError(f"{src.id}: unknown kind {src.kind!r}")
        if src.id in seen:
            raise ValueError(f"duplicate source id {src.id!r}")
        seen.add(src.id)
        if src.enabled or include_disabled:
            sources.append(src)
    return sources
