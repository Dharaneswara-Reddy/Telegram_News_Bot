---
type: CLI Tool
title: Source Management CLI
description: Command-line interface for adding, removing, and listing watched sources without editing code or redeploying.
resource: src/news_bot/manage_sources.py
tags: [cli, source-management, admin]
timestamp: 2026-06-30T00:00:00Z
---

# Purpose

`manage_sources.py` provides a simple CLI for managing the watched source list stored in [Upstash Redis](storage.md). It decouples source configuration from the [watcher script](check_updates.md) — adding or removing a source no longer requires editing Python.

# Usage

```bash
# List all currently watched sources
python -m news_bot.manage_sources list

# Add a new source
python -m news_bot.manage_sources add <id> "<label>" <url> [rolling_log|article_list]

# Remove a source
python -m news_bot.manage_sources remove <id>
```

# Commands

## `list`

Calls [`storage.get_sources()`](storage.md) and prints each source's id, label, URL, and mode.

## `add <id> <label> <url> [mode]`

Delegates to [`storage.add_source()`](storage.md). The `id` should be short, stable, and contain no spaces. Mode defaults to `rolling_log` if omitted.

A newly added source gets the same silent-baseline treatment as defaults — its first check just saves a starting snapshot, no message is sent.

## `remove <id>`

Delegates to [`storage.remove_source()`](storage.md), which also cleans up the source's saved snapshot.

# Environment Requirements

Requires the same `UPSTASH_REDIS_REST_URL` and `UPSTASH_REDIS_REST_TOKEN` environment variables as the [watcher](check_updates.md). Typically run locally from a developer's machine, not in CI.

# Examples

```bash
# Add Mistral's news page
python -m news_bot.manage_sources add mistral_news "Mistral AI" https://mistral.ai/news/ article_list

# Remove it later
python -m news_bot.manage_sources remove mistral_news
```
