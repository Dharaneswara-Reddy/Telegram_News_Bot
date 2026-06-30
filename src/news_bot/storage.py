"""
Storage layer — Upstash Redis (REST API), replacing the committed-JSON-file
approach. Two things live here:

1. The list of sources to watch (was a hardcoded Python list before; now
   data, so adding a source doesn't require touching check_updates.py).
2. Per-source snapshot state (hash + last-seen text), same role
   snapshots.json played before.

Upstash's REST API means every operation is a plain HTTPS request — no
persistent connection, no driver, which suits a script that starts cold
every hour in GitHub Actions.
"""

import json
import logging
import os

import requests

log = logging.getLogger("news-bot.storage")

UPSTASH_URL = os.environ.get("UPSTASH_REDIS_REST_URL")
UPSTASH_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN")

SOURCES_KEY = "newsbot:sources"  # one Redis key, holds the JSON list of all sources
SNAPSHOT_KEY_PREFIX = "newsbot:snapshot:"  # one Redis key per source id, holds its snapshot

REQUEST_TIMEOUT = 15


def _require_config():
    if not UPSTASH_URL or not UPSTASH_TOKEN:
        raise RuntimeError(
            "UPSTASH_REDIS_REST_URL / UPSTASH_REDIS_REST_TOKEN are not set. "
            "Storage cannot work without them — see README for setup."
        )


def _redis_get(key: str) -> str | None:
    _require_config()
    resp = requests.get(
        f"{UPSTASH_URL}/get/{key}",
        headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    result = resp.json().get("result")
    return result  # None if key doesn't exist


def _redis_set(key: str, value: str) -> None:
    _require_config()
    resp = requests.post(
        f"{UPSTASH_URL}/set/{key}",
        headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"},
        json=[
            value
        ],  # Upstash REST expects the value as a JSON array element for POST-with-body form
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()


def _redis_delete(key: str) -> None:
    _require_config()
    resp = requests.post(
        f"{UPSTASH_URL}/del/{key}",
        headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()


# ---------------------------------------------------------------------------
# Sources list
# ---------------------------------------------------------------------------

DEFAULT_SOURCES = [
    {
        "id": "anthropic_news",
        "label": "Anthropic",
        "url": "https://www.anthropic.com/news",
        "mode": "article_list",
    },
    {
        "id": "claude_release_notes",
        "label": "Claude (app)",
        "url": "https://support.claude.com/en/articles/12138966-release-notes",
        "mode": "rolling_log",
    },
    {
        "id": "claude_code_changelog",
        "label": "Claude Code",
        "url": "https://code.claude.com/docs/en/changelog",
        "mode": "rolling_log",
    },
    {
        "id": "anthropic_api_release_notes",
        "label": "Claude API",
        "url": "https://docs.anthropic.com/en/release-notes/overview",
        "mode": "rolling_log",
    },
    {
        "id": "openai_news",
        "label": "OpenAI",
        "url": "https://openai.com/news/",
        "mode": "article_list",
    },
    {
        "id": "chatgpt_release_notes",
        "label": "ChatGPT (app)",
        "url": "https://help.openai.com/en/articles/6825453-chatgpt-release-notes",
        "mode": "rolling_log",
    },
    {
        "id": "openai_model_release_notes",
        "label": "OpenAI models",
        "url": "https://help.openai.com/en/articles/9624314-model-release-notes",
        "mode": "rolling_log",
    },
    {
        "id": "gemini_api_changelog",
        "label": "Gemini API",
        "url": "https://ai.google.dev/gemini-api/docs/changelog",
        "mode": "rolling_log",
    },
    {
        "id": "gemini_blog",
        "label": "Google / Gemini",
        "url": "https://blog.google/products-and-platforms/products/gemini/",
        "mode": "article_list",
    },
    {
        "id": "gemini_apps_updates",
        "label": "Gemini (app)",
        "url": "https://gemini.google.com/updates",
        "mode": "rolling_log",
    },
    {
        "id": "m365_copilot_release_notes",
        "label": "Microsoft 365 Copilot",
        "url": "https://learn.microsoft.com/en-us/microsoft-365/copilot/release-notes",
        "mode": "rolling_log",
    },
]


def get_sources() -> list[dict]:
    """Return the current source list. Seeds it with DEFAULT_SOURCES on first
    ever call (so a brand-new Redis instance isn't empty), then reads from
    storage on every call after that — so manual edits via add_source/
    remove_source persist and are picked up automatically.
    """
    raw = _redis_get(SOURCES_KEY)
    if raw is None:
        log.info(
            "No sources found in storage — seeding with %d default sources.",
            len(DEFAULT_SOURCES),
        )
        _redis_set(SOURCES_KEY, json.dumps(DEFAULT_SOURCES))
        return DEFAULT_SOURCES
    return json.loads(raw)


def add_source(source_id: str, label: str, url: str, mode: str = "rolling_log") -> None:
    """Add a new source. mode is 'rolling_log' (changelog-style, accumulating
    page) or 'article_list' (news/blog index with discrete posts) — both use
    the same diff mechanism currently, this just documents intent.
    """
    if mode not in ("rolling_log", "article_list"):
        raise ValueError("mode must be 'rolling_log' or 'article_list'")

    sources = get_sources()
    if any(s["id"] == source_id for s in sources):
        raise ValueError(
            f"A source with id '{source_id}' already exists. Use a different id, or remove it first."
        )

    sources.append({"id": source_id, "label": label, "url": url, "mode": mode})
    _redis_set(SOURCES_KEY, json.dumps(sources))
    log.info("Added source: %s (%s)", label, url)


def remove_source(source_id: str) -> None:
    sources = get_sources()
    remaining = [s for s in sources if s["id"] != source_id]
    if len(remaining) == len(sources):
        raise ValueError(f"No source found with id '{source_id}'.")
    _redis_set(SOURCES_KEY, json.dumps(remaining))
    _redis_delete(f"{SNAPSHOT_KEY_PREFIX}{source_id}")  # clean up its snapshot too
    log.info("Removed source: %s", source_id)


# ---------------------------------------------------------------------------
# Per-source snapshots
# ---------------------------------------------------------------------------


def get_snapshot(source_id: str) -> dict | None:
    raw = _redis_get(f"{SNAPSHOT_KEY_PREFIX}{source_id}")
    if raw is None:
        return None
    return json.loads(raw)


def set_snapshot(source_id: str, snapshot: dict) -> None:
    _redis_set(f"{SNAPSHOT_KEY_PREFIX}{source_id}", json.dumps(snapshot))
