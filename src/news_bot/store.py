"""
Archive of fetched items, kept as one JSON file per UTC day under
``<data_dir>/items/``. In production ``data_dir`` is a checkout of the repo's
``data`` branch, so the archive survives between stateless CI runs without
any external database. Day sharding keeps each run's diff small: only the
last few days' files ever change.
"""

import json
import logging
from datetime import timedelta
from pathlib import Path

from news_bot.textutil import parse_iso, utcnow

log = logging.getLogger("news-bot.store")

# Fields that may legitimately change between fetches of the same item
# (vote counts, an edited headline). Everything else, notably ``ai`` and
# ``fetched``, is kept from the first sighting.
_REFRESHABLE = ("title", "meta", "excerpt", "weight", "source_name", "src_category")


class Store:
    def __init__(self, data_dir: Path, retention_days: int = 120):
        self.dir = Path(data_dir)
        self.items_dir = self.dir / "items"
        self.retention_days = retention_days
        self.items: dict[str, dict] = {}
        self._dirty_days: set[str] = set()

    # -- loading / saving ---------------------------------------------------

    def load(self) -> "Store":
        self.items_dir.mkdir(parents=True, exist_ok=True)
        for path in sorted(self.items_dir.glob("*.json")):
            try:
                for item in json.loads(path.read_text("utf-8")):
                    self.items[item["id"]] = item
            except (json.JSONDecodeError, KeyError) as exc:
                log.error("Skipping unreadable archive file %s: %s", path.name, exc)
        log.info("Loaded %d archived items.", len(self.items))
        return self

    def save(self) -> None:
        self.items_dir.mkdir(parents=True, exist_ok=True)
        by_day: dict[str, list[dict]] = {}
        for item in self.items.values():
            by_day.setdefault(day_of(item), []).append(item)

        for day in sorted(self._dirty_days):
            path = self.items_dir / f"{day}.json"
            rows = sorted(by_day.get(day, []), key=lambda i: (i["published"], i["id"]))
            if rows:
                path.write_text(json.dumps(rows, ensure_ascii=False, indent=0) + "\n", "utf-8")
            elif path.exists():
                path.unlink()
        log.info("Saved %d changed day file(s).", len(self._dirty_days))
        self._dirty_days.clear()

    def read_json(self, name: str, default):
        path = self.dir / name
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text("utf-8"))
        except json.JSONDecodeError:
            log.error("%s is corrupt; starting it fresh.", name)
            return default

    def write_json(self, name: str, value) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / name).write_text(
            json.dumps(value, ensure_ascii=False, indent=1) + "\n", "utf-8"
        )

    # -- mutation -------------------------------------------------------------

    def upsert(self, item: dict) -> bool:
        """Insert or refresh an item. Returns True if it was not seen before."""
        existing = self.items.get(item["id"])
        if existing is None:
            self.items[item["id"]] = item
            self._dirty_days.add(day_of(item))
            return True
        changed = False
        for key in _REFRESHABLE:
            if item.get(key) and item[key] != existing.get(key):
                existing[key] = item[key]
                changed = True
        if changed:
            self._dirty_days.add(day_of(existing))
        return False

    def mark_dirty(self, item: dict) -> None:
        self._dirty_days.add(day_of(item))

    def prune(self) -> int:
        cutoff = utcnow() - timedelta(days=self.retention_days)
        stale = [i for i in self.items.values() if parse_iso(i["published"]) < cutoff]
        for item in stale:
            del self.items[item["id"]]
            self._dirty_days.add(day_of(item))
        return len(stale)

    def recent(self, days: float) -> list[dict]:
        cutoff = utcnow() - timedelta(days=days)
        return [i for i in self.items.values() if parse_iso(i["published"]) >= cutoff]


def day_of(item: dict) -> str:
    return item["published"][:10]
