---
type: Component
title: Storage Layer
description: Thin wrapper around the Upstash Redis REST API managing the sources list and per-source snapshot state via plain HTTPS calls.
resource: src/news_bot/storage.py
tags: [storage, redis, upstash, rest-api, state]
timestamp: 2026-06-30T00:00:00Z
---

# Purpose

`storage.py` provides all state management for the [watcher](check_updates.md). It replaced a committed-JSON-file approach with Upstash Redis, eliminating the need for repo write-back and commit history management.

# Redis Key Layout

| Key | Type | Contents |
|-----|------|----------|
| `newsbot:sources` | String (JSON array) | The full list of source objects — seeded with 11 defaults on first access |
| `newsbot:snapshot:<source_id>` | String (JSON object) | Per-source snapshot: `{hash, text, last_checked}` |

# Low-Level Operations

Three internal functions wrap Upstash's REST API:

- `_redis_get(key)` — `GET /get/<key>`, returns the value string or `None` if key doesn't exist.
- `_redis_set(key, value)` — `POST /set/<key>` with the value as a JSON array element body.
- `_redis_delete(key)` — `POST /del/<key>` for cleanup (used when [removing sources](manage_sources.md)).

All calls use `Bearer` auth with `UPSTASH_REDIS_REST_TOKEN` and a 15-second timeout. The `_require_config()` guard raises `RuntimeError` if credentials are missing.

# Sources CRUD

## `get_sources() → list[dict]`

Returns the current source list. On first-ever call (key doesn't exist), seeds Redis with `DEFAULT_SOURCES` (11 official AI product pages) and returns them.

## `add_source(source_id, label, url, mode)`

Appends a new source. Validates mode is `rolling_log` or `article_list`. Rejects duplicate IDs. Used by [manage_sources CLI](manage_sources.md).

## `remove_source(source_id)`

Removes a source and cleans up its snapshot key. Raises `ValueError` if the ID doesn't exist.

# Snapshot Operations

## `get_snapshot(source_id) → dict | None`

Returns the parsed snapshot object or `None` if this source has never been checked.

## `set_snapshot(source_id, snapshot)`

Saves the snapshot (hash + full text + timestamp). Called by the [watcher](check_updates.md) after successful processing.

# Default Sources

The 11 default sources cover:
- **Anthropic**: news page, Claude app release notes, Claude Code changelog, API release notes
- **OpenAI**: news page, ChatGPT release notes, model release notes
- **Google/Gemini**: API changelog, blog, app updates
- **Microsoft**: 365 Copilot release notes
