"""
Command-line entry point.

    news-bot run      fetch → store → summarise → brief → build site (the CI job)
    news-bot build    rebuild the site from stored data only, no network
    news-bot sources  fetch every source once and print a health table
    news-bot serve    preview the built site at http://localhost:8000
"""

import argparse
import contextlib
import functools
import hashlib
import http.server
import logging
import os
import sys
import time
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from news_bot.build import SITE_DAYS, build_site
from news_bot.cluster import build_stories
from news_bot.enrich import daily_brief, enrich_items, weekly_digest
from news_bot.fetchers import fetch_all
from news_bot.llm import GroqClient
from news_bot.sources import load_sources
from news_bot.store import Store
from news_bot.textutil import iso, parse_iso, utcnow

log = logging.getLogger("news-bot")


def _local_date() -> str:
    tz = ZoneInfo(os.environ.get("NEWS_TZ") or "UTC")
    return utcnow().astimezone(tz).date().isoformat()


def _source_status(sources, results, previous: dict) -> dict:
    now = iso(utcnow())
    status = dict(previous)
    for res in results:
        prev = status.get(res.source.id, {})
        entry = {**prev, "last_checked": now, "seconds": res.seconds}
        if res.error:
            entry["error"] = res.error
            entry["fails"] = prev.get("fails", 0) + 1
        else:
            entry.update(last_ok=now, error="", fails=0, last_count=len(res.items))
        status[res.source.id] = entry
    # Forget sources that were removed from sources.toml.
    live = {s.id for s in sources}
    return {k: v for k, v in status.items() if k in live}


def _public_sources(sources, status: dict, items: list[dict]) -> list[dict]:
    newest: dict[str, str] = {}
    counts: dict[str, int] = {}
    for item in items:
        sid = item["source"]
        counts[sid] = counts.get(sid, 0) + 1
        if item["published"] > newest.get(sid, ""):
            newest[sid] = item["published"]
    rows = []
    for s in sources:
        st = status.get(s.id, {})
        rows.append(
            {
                "id": s.id,
                "name": s.name,
                "category": s.category,
                "weight": s.weight,
                "homepage": s.homepage or s.url,
                "ok": not st.get("error"),
                "error": st.get("error", ""),
                "last_ok": st.get("last_ok"),
                "newest": newest.get(s.id),
                "items_30d": counts.get(s.id, 0),
            }
        )
    return rows


def _bullets(brief: dict) -> list[dict]:
    if "bullets" in brief:
        return brief["bullets"]
    return [b for sec in brief.get("sections", []) for b in sec["bullets"]]


def _with_refs(brief: dict, stories: list[dict], basis: str) -> dict:
    """Snapshot the title/url of every cited story so archived briefs keep
    working links after those stories age out of the site."""
    by_id = {s["id"]: s for s in stories}
    ids = {i for b in _bullets(brief) for i in b["ids"]}
    brief["refs"] = {
        i: {"title": by_id[i]["title"], "url": by_id[i]["url"], "source": by_id[i]["source_name"]}
        for i in ids
        if i in by_id
    }
    brief["generated"] = iso(utcnow())
    brief["basis"] = basis
    return brief


def _window(stories: list[dict], hours: int, minimum: int) -> list[dict]:
    """Top stories of the last ``hours``, widening the window on quiet days."""
    now = utcnow()
    picked: list[dict] = []
    for h in (hours, hours * 1.5, hours * 2):
        cutoff = now - timedelta(hours=h)
        picked = [s for s in stories if parse_iso(s["published"]) >= cutoff]
        if len(picked) >= minimum:
            break
    return sorted(picked, key=lambda s: s["score"], reverse=True)


def _basis(stories: list[dict]) -> str:
    return hashlib.sha1(",".join(sorted(s["id"] for s in stories)).encode()).hexdigest()[:10]


def update_briefs(llm: GroqClient, stories: list[dict], briefs: dict) -> None:
    today = _local_date()
    daily = briefs.setdefault("daily", {})
    top = _window(stories, 24, 8)[:30]
    basis = _basis(top)
    # Only rewrite the brief when the set of top stories actually moved.
    if daily.get(today, {}).get("basis") != basis:
        brief = daily_brief(llm, top)
        if brief:
            daily[today] = _with_refs(brief, top, basis)

    weekly = briefs.setdefault("weekly", {})
    latest = weekly[max(weekly)] if weekly else None
    if latest is None or utcnow() - parse_iso(latest["generated"]) > timedelta(hours=20):
        week = _window(stories, 24 * 7, 20)[:45]
        digest = weekly_digest(llm, week)
        if digest:
            weekly[today] = _with_refs(digest, week, _basis(week))

    for bucket, keep in (("daily", 90), ("weekly", 30)):
        for key in sorted(briefs[bucket])[:-keep]:
            del briefs[bucket][key]


def _build(store, sources, state, briefs, stories, out) -> None:
    build_site(
        Path(out),
        stories,
        sources=_public_sources(sources, state.get("sources", {}), store.recent(30)),
        briefs=briefs,
        trending_models=state.get("trending_models", []),
        run=state.get("last_run", {}),
    )


def cmd_run(args) -> int:
    started = time.monotonic()
    sources = load_sources()
    store = Store(Path(args.data_dir)).load()
    state = store.read_json("state.json", {})
    briefs = store.read_json("briefs.json", {})

    results = fetch_all(sources)
    new = 0
    for res in results:
        for item in res.items:
            new += store.upsert(item)
    pruned = store.prune()
    state["sources"] = _source_status(sources, results, state.get("sources", {}))
    trending = next((r.extra["trending_models"] for r in results if r.extra), None)
    if trending:
        state["trending_models"] = trending
    log.info("%d new items, %d pruned, %d in archive.", new, pruned, len(store.items))

    llm = GroqClient.from_env(deadline=started + args.llm_minutes * 60)
    if llm is None:
        log.warning("GROQ_API_KEY not set; publishing without summaries or briefs.")
    else:
        for item in enrich_items(llm, list(store.items.values()), limit=args.max_enrich):
            store.mark_dirty(item)

    stories = build_stories(store.recent(SITE_DAYS))
    if llm is not None:
        update_briefs(llm, stories, briefs)
        log.info("Groq: %d calls, %d tokens.", llm.calls, llm.tokens)

    store.save()
    failed = [r.source.id for r in results if r.error]
    state["last_run"] = {
        "at": iso(utcnow()),
        "new_items": new,
        "sources_ok": len(results) - len(failed),
        "sources_failed": failed,
        "llm_calls": llm.calls if llm else 0,
        "seconds": round(time.monotonic() - started, 1),
    }
    store.write_json("state.json", state)
    store.write_json("briefs.json", briefs)

    _build(store, sources, state, briefs, stories, args.out)
    if len(failed) == len(results):
        log.error("Every source failed — treating the run as broken.")
        return 1
    return 0


def cmd_build(args) -> int:
    sources = load_sources()
    store = Store(Path(args.data_dir)).load()
    state = store.read_json("state.json", {})
    briefs = store.read_json("briefs.json", {})
    _build(store, sources, state, briefs, build_stories(store.recent(SITE_DAYS)), args.out)
    return 0


def cmd_sources(args) -> int:
    sources = load_sources()
    if args.only:
        sources = [s for s in sources if s.id in args.only]
    results = fetch_all(sources)
    width = max((len(s.id) for s in sources), default=10)
    for res in sorted(results, key=lambda r: (not r.error, r.source.id)):
        newest = max((i["published"] for i in res.items), default="-")
        state = f"FAIL {res.error[:60]}" if res.error else f"ok   {len(res.items):3d} items"
        print(f"{res.source.id:<{width}}  {state:<40} newest {newest[:10]}  {res.seconds:5.1f}s")
    return 1 if any(r.error for r in results) else 0


def cmd_serve(args) -> int:
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=args.out)
    with http.server.ThreadingHTTPServer(("127.0.0.1", args.port), handler) as httpd:
        print(f"Serving {args.out} at http://localhost:{args.port}  (Ctrl+C to stop)")
        with contextlib.suppress(KeyboardInterrupt):
            httpd.serve_forever()
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    parser = argparse.ArgumentParser(prog="news-bot", description="AI news digest builder")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--data-dir", default=os.environ.get("NEWS_DATA_DIR", "data-store"))
        p.add_argument("--out", default="_site")

    p_run = sub.add_parser("run", help="fetch, summarise and build the site")
    common(p_run)
    p_run.add_argument("--max-enrich", type=int, default=int(os.environ.get("MAX_ENRICH", 200)))
    p_run.add_argument(
        "--llm-minutes", type=float, default=float(os.environ.get("LLM_MINUTES", 14))
    )
    p_run.set_defaults(func=cmd_run)

    p_build = sub.add_parser("build", help="rebuild the site from stored data")
    common(p_build)
    p_build.set_defaults(func=cmd_build)

    p_src = sub.add_parser("sources", help="check every source and print a health table")
    p_src.add_argument("only", nargs="*", help="limit to these source ids")
    p_src.set_defaults(func=cmd_sources)

    p_serve = sub.add_parser("serve", help="preview the built site locally")
    p_serve.add_argument("--out", default="_site")
    p_serve.add_argument("--port", type=int, default=8000)
    p_serve.set_defaults(func=cmd_serve)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
