---
type: Architecture
title: System Architecture
description: Two-hourly stateless pipeline that collects AI news from feeds and APIs, summarises and ranks it with Groq, clusters duplicate coverage, and publishes a static reading site.
tags: [architecture, pipeline, github-actions, github-pages, groq]
timestamp: 2026-10-02T00:00:00Z
---

# Overview

AI Pulse replaced the earlier Telegram watcher (which diffed HTML pages of a
handful of vendors). It reads only structured sources — RSS/Atom feeds,
the Hacker News Algolia API, Hugging Face Daily Papers and trending-model
APIs — listed in `src/news_bot/sources.toml`, and publishes a website
instead of sending messages.

# Pipeline (`news-bot run`, every two hours in `.github/workflows/news.yml`)

1. **Fetch** (`fetchers.py`) — all sources concurrently; each item gets a
   canonical URL key and a stable id. Failures are recorded per source and
   shown on the Sources page; they never abort the run.
2. **Archive** (`store.py`) — items are upserted into one JSON file per UTC
   day under `items/`. The archive lives on the `data` branch, force-pushed
   as a single commit each run so history never accumulates. Retention 120
   days; the site shows 60.
3. **Enrich** (`enrich.py`, `llm.py`) — unsummarised items, most valuable
   first, go to Groq in batches of 8 for a summary, "why it matters", topic
   category and importance (1–5). The client rotates across models because
   each has its own free-tier token bucket, honours rate-limit headers, and
   drops retired model ids. Whatever doesn't fit the run's budget waits for
   the next run.
4. **Cluster** (`cluster.py`) — union-find over shared canonical URLs and
   similar headlines (rare shared names, matching version numbers) merges
   coverage of one event into one story with "also reported by" links.
   Newsletter roundups are excluded from headline matching.
5. **Brief** — a daily brief (rewritten only when the top stories change)
   and a weekly review (at most daily), each citing story ids; cited titles
   and URLs are snapshotted so archived briefs keep working links.
6. **Build** (`build.py`) — copies `site/` and writes `data/index.json`,
   `data/days/<date>.json`, `data/briefs.json` and `data/catchup.json`;
   deployed to GitHub Pages.

# Front end (`site/`)

Static HTML/CSS/JS with no build step, styled as a broadsheet. It loads
`index.json`, then only the day files for the range being viewed. Read,
saved and catch-up progress state live in the browser's localStorage.

# Curated content (`content/`)

`catchup.json` (Oct 2025 – Oct 2026 year-in-review with glossary) and
`conferences.json` (upcoming venues and deadlines) are hand-researched
static files, refreshed manually.
