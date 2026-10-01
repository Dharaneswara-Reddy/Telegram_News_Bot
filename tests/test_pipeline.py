"""Tests for the fetch → store → enrich → cluster → build pipeline."""

import json
from datetime import timedelta
from unittest.mock import MagicMock

import pytest

from news_bot import build, enrich, fetchers, llm
from news_bot.cluster import build_stories, cluster_items
from news_bot.sources import Source, load_sources
from news_bot.store import Store
from news_bot.textutil import iso, utcnow


def src(sid="lab", weight=3, category="labs", **kw) -> Source:
    return Source(
        id=sid,
        name=sid.title(),
        url=f"https://{sid}.example/feed",
        category=category,
        weight=weight,
        **kw,
    )


def item(title, *, source=None, url=None, hours_ago=1, kind="article", meta=None, ai=None):
    s = source or src()
    it = fetchers.make_item(
        s,
        url=url or f"https://{s.id}.example/{abs(hash(title))}",
        title=title,
        published=utcnow() - timedelta(hours=hours_ago),
        kind=kind,
        meta=meta,
    )
    it["ai"] = ai
    return it


def resp(*, json_data=None, content=b"", status=200, headers=None, text=""):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = json_data
    r.content = content
    r.text = text or (json.dumps(json_data) if json_data is not None else "")
    r.headers = headers or {}
    r.raise_for_status = MagicMock()
    return r


# ---------------------------------------------------------------- sources


def test_bundled_sources_file_is_valid():
    sources = load_sources()
    assert len(sources) > 30
    assert len({s.id for s in sources}) == len(sources)
    assert {s.category for s in sources} >= {"labs", "research", "opensource", "news"}


def test_source_include_exclude():
    s = src(include="model", exclude=r"rc\d")
    assert s.keeps("New model released")
    assert not s.keeps("model v1.0rc1")
    assert not s.keeps("Company picnic")


# ---------------------------------------------------------------- fetchers

RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
<item><title>Fresh post</title><link>https://lab.example/fresh</link>
<pubDate>%s</pubDate><description>&lt;p&gt;Body text&lt;/p&gt;</description></item>
<item><title>Ancient post</title><link>https://lab.example/old</link>
<pubDate>Mon, 01 Jan 2024 00:00:00 GMT</pubDate></item>
</channel></rss>"""


def test_fetch_feed_parses_and_applies_age_cutoff():
    now = utcnow().strftime("%a, %d %b %Y %H:%M:%S GMT").encode()
    session = MagicMock()
    session.get.return_value = resp(content=RSS % now)
    result = fetchers.fetch_feed(session, src())
    assert [i["title"] for i in result.items] == ["Fresh post"]
    assert result.items[0]["excerpt"] == "Body text"
    assert result.items[0]["kind"] == "article"


def test_fetch_feed_names_github_release_titles():
    feed = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>r</title>
    <entry><title>v0.11.0</title><link href="https://github.com/o/vllm/releases/tag/v0.11.0"/>
    <updated>%s</updated><content>notes</content></entry></feed>""" % iso(utcnow()).encode()
    session = MagicMock()
    session.get.return_value = resp(content=feed)
    s = Source(
        id="gh", name="vLLM", url="https://github.com/o/vllm/releases.atom", category="opensource"
    )
    out = fetchers.fetch_feed(session, s).items
    assert out[0]["title"] == "vllm v0.11.0"
    assert out[0]["kind"] == "release"


def test_fetch_feed_title_strip_option():
    feed = RSS % utcnow().strftime("%a, %d %b %Y %H:%M:%S GMT").encode()
    feed = feed.replace(b"Fresh post", b"Oct 1, 2026ScienceFresh post")
    session = MagicMock()
    session.get.return_value = resp(content=feed)
    s = src(options={"title_strip": r"^[A-Z][a-z]{2} \d{1,2}, \d{4}(Science)?"})
    assert fetchers.fetch_feed(session, s).items[0]["title"] == "Fresh post"


def test_fetch_hn_keeps_only_ai_titles():
    now = iso(utcnow())
    hits = [
        {
            "objectID": "1",
            "title": "Show HN: An LLM that writes SQL",
            "url": "https://a.dev",
            "points": 300,
            "num_comments": 80,
            "created_at": now,
        },
        {
            "objectID": "2",
            "title": "My grandmother's bread recipe",
            "url": "https://b.dev",
            "points": 900,
            "num_comments": 10,
            "created_at": now,
        },
        {
            "objectID": "3",
            "title": "Ask HN: Is Claude better at Rust?",
            "url": None,
            "points": 120,
            "num_comments": 50,
            "created_at": now,
        },
    ]
    session = MagicMock()
    session.get.return_value = resp(json_data={"hits": hits})
    out = fetchers.fetch_hn(session, src("hn", kind="hn", category="community"))
    assert [i["meta"]["points"] for i in out.items] == [300, 120]
    assert out.items[1]["url"] == "https://news.ycombinator.com/item?id=3"


def test_fetch_hf_trending_skips_gamed_likes_and_flags_new_models():
    rows = [
        {"id": "spam/model", "likes": 5000, "downloads": 0, "createdAt": iso(utcnow())},
        {
            "id": "org/new",
            "likes": 300,
            "downloads": 9000,
            "createdAt": iso(utcnow()),
            "pipeline_tag": "text-generation",
        },
        {"id": "org/old", "likes": 900, "downloads": 90000, "createdAt": "2024-01-01T00:00:00Z"},
    ]
    session = MagicMock()
    session.get.return_value = resp(json_data=rows)
    out = fetchers.fetch_hf_trending(session, src("hf", kind="hf_trending", category="opensource"))
    assert [m["id"] for m in out.extra["trending_models"]] == ["org/new", "org/old"]
    assert [i["title"] for i in out.items] == ["org/new is trending on Hugging Face"]


def test_fetch_all_reports_failures_without_raising(monkeypatch):
    def boom(session, source):
        raise RuntimeError("down")

    monkeypatch.setitem(fetchers.ADAPTERS, "feed", boom)
    (result,) = fetchers.fetch_all([src()])
    assert result.error == "down"
    assert result.items == []


def test_future_dates_are_clamped_to_now():
    it = fetchers.make_item(
        src(), url="https://x.dev/a", title="t", published=utcnow() + timedelta(days=3)
    )
    assert it["published"] <= iso(utcnow() + timedelta(minutes=1))


# ---------------------------------------------------------------- store


def test_store_roundtrip_upsert_and_refresh(tmp_path):
    store = Store(tmp_path).load()
    first = item("Hello", meta={"points": 10})
    assert store.upsert(first) is True
    first["ai"] = {"summary": "s", "why": "", "category": "models", "importance": 3, "tags": []}
    store.save()

    again = Store(tmp_path).load()
    refreshed = dict(first, meta={"points": 99}, ai=None)
    assert again.upsert(refreshed) is False
    kept = again.items[first["id"]]
    assert kept["meta"]["points"] == 99
    assert kept["ai"]["summary"] == "s"  # enrichment survives refreshes


def test_store_prune(tmp_path):
    store = Store(tmp_path, retention_days=10).load()
    store.upsert(item("old", hours_ago=24 * 30))
    store.upsert(item("new"))
    assert store.prune() == 1
    store.save()
    assert [i["title"] for i in Store(tmp_path).load().items.values()] == ["new"]


# ---------------------------------------------------------------- clustering


def test_same_url_from_two_sources_clusters():
    a = item("OpenAI ships GPT-7", source=src("openai", 5), url="https://openai.com/gpt-7")
    b = item(
        "GPT-7",
        source=src("hn", 3, "community"),
        url="https://openai.com/gpt-7/?utm_source=hn",
        meta={"points": 900, "hn_url": "https://news.ycombinator.com/item?id=1"},
    )
    (story,) = build_stories([a, b])
    assert story["source_name"] == "Openai"  # highest-weight source leads
    assert story["points"] == 900
    assert story["also"][0]["source_name"] == "Hn"


def test_rare_name_headlines_cluster_across_outlets():
    stories = [
        item("Gemini 4 Argon: our next era of frontier intelligence", source=src("deepmind", 5)),
        item("Gemini 4 Argon", source=src("hn", 3, "community")),
        item(
            "Google releases Gemini 4 Argon, called its most powerful model yet",
            source=src("tc", 3, "news"),
        ),
        item("Apple sues Samsung over phone design", source=src("verge", 3, "news")),
    ]
    assert sorted(len(g) for g in cluster_items(stories)) == [1, 3]


def test_newsletter_roundups_do_not_glue_stories():
    roundup = item(
        "[AINews] Gemini 4 Argon, DevDay Dots, and AMD buys World Labs",
        source=src("latent", 4, "analysis"),
    )
    a = item("Gemini 4 Argon", source=src("hn", 3, "community"))
    b = item("AMD acquires World Labs AI startup", source=src("ars", 3, "news"))
    assert len(cluster_items([roundup, a, b])) == 3


def test_offtopic_items_are_dropped_from_stories():
    junk = item(
        "Best mattress deals",
        ai={"summary": "x", "why": "", "category": "other", "importance": 1, "tags": []},
    )
    assert build_stories([junk]) == []


# ---------------------------------------------------------------- enrichment


class FakeLLM:
    def __init__(self, reply):
        self.reply = reply

    def chat_json(self, system, user, max_tokens=0):
        prompt = json.loads(user)
        return self.reply(prompt) if callable(self.reply) else self.reply


def test_enrich_items_applies_valid_results_and_counts_misses():
    a, b = item("A"), item("B")

    def reply(prompt):
        first = prompt["items"][0]["id"]
        return {
            "items": [
                {
                    "id": first,
                    "summary": "Did a thing.",
                    "why": "",
                    "category": "MODELS",
                    "importance": 9,
                    "tags": ["X", "", "y"],
                }
            ]
        }

    touched = enrich.enrich_items(FakeLLM(reply), [a, b], limit=10)
    assert len(touched) == 2
    done = [i for i in (a, b) if i["ai"]]
    missed = [i for i in (a, b) if not i["ai"]]
    assert done[0]["ai"] == {
        "summary": "Did a thing.",
        "why": "",
        "category": "models",
        "importance": 5,
        "tags": ["x", "y"],
    }
    assert missed[0]["ai_attempts"] == 1


def test_pending_skips_old_low_weight_and_given_up_items():
    old_low = item("old", hours_ago=24 * 20, source=src("blog", 2))
    old_high = item("old lab", hours_ago=24 * 20, source=src("lab", 5))
    gave_up = item("x")
    gave_up["ai_attempts"] = enrich.MAX_ATTEMPTS
    fresh = item("fresh")
    titles = {i["title"] for i in enrich.pending([old_low, old_high, gave_up, fresh])}
    assert titles == {"old lab", "fresh"}


def test_daily_brief_drops_bullets_citing_unknown_ids():
    stories = build_stories([item(f"Story number {n}", source=src(f"s{n}")) for n in range(4)])
    good = stories[0]["id"]
    fake = FakeLLM(
        {
            "headline": "H",
            "bullets": [{"text": "ok", "ids": [good]}, {"text": "made up", "ids": ["nope"]}],
        }
    )
    brief = enrich.daily_brief(fake, stories)
    assert brief == {"headline": "H", "bullets": [{"text": "ok", "ids": [good]}]}


def test_enrich_stops_cleanly_when_llm_unavailable():
    class Down:
        def chat_json(self, *a, **k):
            raise llm.LLMUnavailableError("quota")

    items = [item("A")]
    assert enrich.enrich_items(Down(), items, limit=5) == []
    assert items[0]["ai"] is None


# ---------------------------------------------------------------- llm client


def test_duration_parsing():
    assert llm._duration("1m26.4s") == pytest.approx(86.4)
    assert llm._duration("250ms") == pytest.approx(0.25)
    assert llm._duration(None) == 0.0


def test_parse_json_object_tolerates_fences_and_think_blocks():
    assert llm.parse_json_object('<think>hmm</think>```json\n{"a": 1}\n```') == {"a": 1}
    assert llm.parse_json_object('Sure! {"a": 2} hope that helps') == {"a": 2}
    with pytest.raises(ValueError):
        llm.parse_json_object("[1, 2]")


def _completion(content):
    return resp(
        json_data={"choices": [{"message": {"content": content}}], "usage": {"total_tokens": 10}},
        headers={"x-ratelimit-remaining-tokens": "7000"},
    )


def test_chat_json_falls_back_to_next_model_on_daily_limit():
    session = MagicMock()
    session.post.side_effect = [
        resp(status=429, text="Rate limit reached ... tokens per day (TPD)"),
        _completion('{"ok": true}'),
    ]
    client = llm.GroqClient("k", ["m1", "m2"], session=session)
    assert client.chat_json("s", "u") == {"ok": True}
    assert "m1" in client.exhausted
    assert session.post.call_args.kwargs["json"]["model"] == "m2"


def test_chat_json_drops_retired_model():
    session = MagicMock()
    session.post.side_effect = [
        resp(status=404, text='{"error": "model_decommissioned"}'),
        _completion('{"ok": 1}'),
    ]
    client = llm.GroqClient("k", ["old", "new"], session=session)
    assert client.chat_json("s", "u") == {"ok": 1}
    assert "old" in client.exhausted


def test_refresh_live_models_keeps_only_live_ids():
    session = MagicMock()
    session.get.return_value = resp(json_data={"data": [{"id": "b"}, {"id": "whisper-large-v3"}]})
    client = llm.GroqClient("k", ["a", "b"], session=session)
    client.refresh_live_models()
    assert client.models == ["b"]


def test_refresh_live_models_falls_back_when_all_retired():
    session = MagicMock()
    session.get.return_value = resp(
        json_data={"data": [{"id": "openai/gpt-oss-999b"}, {"id": "whisper-large-v3"}]}
    )
    client = llm.GroqClient("k", ["gone"], session=session)
    client.refresh_live_models()
    assert client.models == ["openai/gpt-oss-999b"]


def test_all_models_exhausted_raises():
    client = llm.GroqClient("k", ["m"], session=MagicMock())
    client.exhausted.add("m")
    with pytest.raises(llm.LLMUnavailableError):
        client.chat_json("s", "u")


# ---------------------------------------------------------------- site build


def test_build_site_writes_index_and_day_files(tmp_path, monkeypatch):
    content = tmp_path / "content"
    content.mkdir()
    (content / "conferences.json").write_text(
        json.dumps(
            [
                {"name": "Past", "start": "2020-01-01", "end": "2020-01-02"},
                {"name": "Future", "start": "2099-01-01", "end": "2099-01-02"},
            ]
        )
    )
    monkeypatch.setattr(build, "CONTENT_DIR", content)
    stories = build_stories(
        [item("Alpha launch", source=src("a")), item("Beta paper", source=src("b"), hours_ago=30)]
    )
    out = tmp_path / "site"
    build.build_site(
        out,
        stories,
        sources=[],
        briefs={"daily": {"2026-10-02": {"bullets": []}}},
        trending_models=[],
        run={},
    )
    index = json.loads((out / "data" / "index.json").read_text())
    assert sum(d["count"] for d in index["days"]) == 2
    assert [c["name"] for c in index["conferences"]] == ["Future"]
    assert index["brief"] == {"bullets": []}
    for d in index["days"]:
        assert (out / "data" / "days" / f"{d['date']}.json").exists()
    assert (out / "index.html").exists()
