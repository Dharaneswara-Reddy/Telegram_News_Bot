"""
Render the static site: copy the UI from ``site/`` and write the JSON it
reads under ``data/``.

    data/index.json          run metadata, briefs, source health, day list
    data/days/<date>.json    clustered stories first published that UTC day
    data/briefs.json         archive of past daily briefs and weekly digests
    data/catchup.json        curated "what you missed" guide (static content)

The UI loads ``index.json`` first, then only the day files for the range
being viewed, so the page stays light however long the archive gets.
"""

import json
import logging
import shutil
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from news_bot.enrich import CATEGORIES
from news_bot.textutil import iso, parse_iso, utcnow

log = logging.getLogger("news-bot.build")

ROOT = Path(__file__).resolve().parents[2]
SITE_SRC = ROOT / "site"
CONTENT_DIR = ROOT / "content"
SITE_DAYS = 60


def _dump(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")), "utf-8")


def _conferences(now: datetime) -> list[dict]:
    path = CONTENT_DIR / "conferences.json"
    if not path.exists():
        return []
    rows = json.loads(path.read_text("utf-8"))
    cutoff = (now - timedelta(days=3)).date().isoformat()
    upcoming = [r for r in rows if (r.get("end") or r.get("start") or "9999") >= cutoff]
    return sorted(upcoming, key=lambda r: r.get("start") or "9999")


def build_site(
    out_dir: Path,
    stories: list[dict],
    *,
    sources: list[dict],
    briefs: dict,
    trending_models: list[dict],
    run: dict,
) -> None:
    out_dir = Path(out_dir)
    if out_dir.exists():
        shutil.rmtree(out_dir)
    shutil.copytree(SITE_SRC, out_dir)
    data = out_dir / "data"

    now = utcnow()
    cutoff = now - timedelta(days=SITE_DAYS)
    by_day: dict[str, list[dict]] = defaultdict(list)
    for story in stories:
        if parse_iso(story["published"]) >= cutoff:
            by_day[story["published"][:10]].append(story)
    for day, rows in by_day.items():
        rows.sort(key=lambda s: s["score"], reverse=True)
        _dump(data / "days" / f"{day}.json", rows)

    daily = briefs.get("daily", {})
    weekly = briefs.get("weekly", {})
    days = [{"date": d, "count": len(r)} for d, r in by_day.items()]

    index = {
        "generated": iso(now),
        "run": run,
        "categories": CATEGORIES,
        "days": sorted(days, key=lambda d: d["date"], reverse=True),
        "brief": daily[max(daily)] if daily else None,
        "weekly": weekly[max(weekly)] if weekly else None,
        "sources": sources,
        "trending_models": trending_models,
        "conferences": _conferences(now),
    }
    _dump(data / "index.json", index)
    _dump(
        data / "briefs.json",
        {
            "daily": [daily[k] for k in sorted(daily, reverse=True)][:60],
            "weekly": [weekly[k] for k in sorted(weekly, reverse=True)][:26],
        },
    )
    catchup = CONTENT_DIR / "catchup.json"
    if catchup.exists():
        shutil.copy(catchup, data / "catchup.json")
    (out_dir / ".nojekyll").write_text("")
    total = sum(map(len, by_day.values()))
    log.info("Built site: %d stories across %d days -> %s", total, len(by_day), out_dir)
