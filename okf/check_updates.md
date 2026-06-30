---
type: Script
title: Watcher Script
description: Main scheduled script that fetches configured pages, diffs content against saved snapshots, summarizes new content with Groq, and posts to Telegram.
resource: src/news_bot/check_updates.py
tags: [watcher, scheduler, diff, pipeline, entry-point]
timestamp: 2026-06-30T00:00:00Z
---

# Purpose

`check_updates.py` is the main entry point, run hourly via GitHub Actions cron. It orchestrates the entire fetch → diff → summarize → notify pipeline described in the [architecture](architecture.md).

# Configuration

All configuration is via environment variables (no config files):

| Variable | Purpose |
|----------|---------|
| `GROQ_API_KEY` | Authenticates with the [Groq API](groq.md) |
| `TELEGRAM_BOT_TOKEN` | Authenticates the [Telegram bot](telegram.md) |
| `TELEGRAM_CHAT_ID` | Target chat for [Telegram messages](telegram.md) |
| `UPSTASH_REDIS_REST_URL` | [Upstash Redis](storage.md) endpoint |
| `UPSTASH_REDIS_REST_TOKEN` | [Upstash Redis](storage.md) auth token |

The script validates all five are present at startup and exits with code 1 if any are missing.

# Key Functions

## `fetch_page_text(url) → str`

Fetches a URL via HTTP GET and converts HTML to plain text using regex tag-stripping. Deliberately simple — no headless browser, no full HTML parser. Removes `<script>`/`<style>` blocks, strips tags, decodes `&nbsp;`/`&amp;`, collapses whitespace.

## `text_hash(text) → str`

SHA-256 hex digest of the text content. Used for quick change detection before running the more expensive diff.

## `extract_new_content(old_text, new_text, mode) → str`

Line-level diff using `difflib.SequenceMatcher`. Extracts lines present in new but not old (insert/replace opcodes). Filters noise lines ≤25 characters, deduplicates while preserving order, and truncates at 6000 characters with a note.

The `mode` parameter (`rolling_log` or `article_list`) documents source type intent but both currently use the same diff mechanism.

## `main()`

Orchestrates the full run:
1. Validates environment variables.
2. Loads sources from [storage](storage.md).
3. For each source: fetch → hash-compare → diff → [summarize](groq.md) → [send](telegram.md) → update snapshot.
4. Exits with code 2 if any source had failures (non-fatal — flags the GH Action run without killing the workflow).

# Error Handling

- **Fetch failure**: Logged, source skipped, snapshot untouched (retried next run).
- **Groq failure**: Logged, snapshot deliberately NOT updated so the same fragment retries.
- **Telegram failure**: Same — snapshot not updated, retry next run.
- **No substantive change**: Hash changed but diff produced nothing meaningful (e.g. timestamp-only). Snapshot updated silently.
