---
type: Architecture
title: System Architecture
description: Hourly stateless pipeline that fetches AI product pages, diffs against saved snapshots, summarizes changes with an LLM, and posts alerts to Telegram.
tags: [architecture, pipeline, github-actions, stateless]
timestamp: 2026-06-30T00:00:00Z
---

# Overview

The AI release-notes watcher is a scheduled automation that monitors official AI company pages (Anthropic, OpenAI, Google/Gemini, Microsoft Copilot, and user-added sources) for new content. It runs once per hour via a GitHub Actions cron job.

# Pipeline Flow

Each run follows a five-step pipeline for every configured source:

1. **Fetch** — HTTP GET the page, regex-strip HTML to plain text.
2. **Snapshot compare** — SHA-256 hash check against the previous run's saved text (stored in [Upstash Redis](storage.md)).
3. **Diff** — If the hash changed, extract only the genuinely new lines using `difflib.SequenceMatcher`. Filter noise (lines ≤25 chars), deduplicate, and cap at 6000 chars.
4. **Summarize** — Send the new fragment to [Groq](groq.md) for a structured explanation + use-case summary.
5. **Notify** — Post the result to [Telegram](telegram.md) in a four-field HTML format.

# Stateless Design

The script starts cold every hour — no persistent process, no daemon, no database connection pool. All state lives in Upstash Redis via plain HTTPS REST calls (see [storage layer](storage.md)). This means:

- No committed state in the repo (no JSON files to manage, no write-back permissions needed).
- The GitHub Action needs no special permissions beyond reading the repo.
- Failure is retry-friendly: if Groq or Telegram fails, the snapshot is deliberately **not** updated, so the same content gets retried next hour.

# First-Run Behavior

The first time a source is ever checked (whether from the default 11 or newly added via [manage_sources](manage_sources.md)), the script saves a silent baseline — it does not dump a summary of the entire existing page. Only content that appears **after** the baseline is treated as new.

# Components

- [check_updates.py](check_updates.md) — the main script, run on schedule
- [storage.py](storage.md) — Upstash Redis wrapper
- [manage_sources.py](manage_sources.md) — CLI for source management
- [Groq integration](groq.md) — LLM summarization
- [Telegram integration](telegram.md) — message delivery
