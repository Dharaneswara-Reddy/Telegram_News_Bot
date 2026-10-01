# AI Pulse

A personal broadsheet for artificial intelligence. Every two hours it
collects news, research and releases from 60+ machine-readable sources,
de-duplicates the same story across outlets, has an LLM summarise and rank
everything, and publishes a static reading site — no notifications, no
inbox, just a front page to open when you want to catch up.

**Live site:** https://dharaneswara-reddy.github.io/Telegram_News_Bot/

## What you get

- **Front page** — the lead story, *The Briefing* (an LLM-written rundown
  of the last 24 hours with links to every source), top stories set in
  newspaper columns, fresh research, trending open models and upcoming
  conferences.
- **All news** — every story, filterable by topic (models, research, open
  source, agents & dev tools, industry, policy, events), time window and
  importance. Search across 60 days.
- **Research** — Hugging Face Daily Papers ranked by community upvotes, plus
  lab and analyst research posts.
- **Open source** — what's trending on Hugging Face and notable releases
  (vLLM, Ollama, Transformers, Claude Code, Codex, Gemini CLI, PyTorch…).
- **Conferences** — NeurIPS, ICLR, ICML, CVPR, ACL and the rest, with
  countdowns and paper deadlines.
- **Catch up** — a guided tour of October 2025 → October 2026 for anyone
  who looked away for a year, with progress tracking.
- **Briefs** — archive of daily briefings and weekly reviews.
- **Saved / read state** — kept in your browser. Keyboard: `j`/`k` move,
  `o` open, `m` mark read, `s` save, `/` search.
- **Palettes** — six colour schemes (Broadsheet, Salmon, Riso, Cobalt,
  Phosphor, Nocturne), each with a day and night edition. Pick one from the
  swatch button in the section bar, press `p` to cycle, or compare them all
  at `#/palettes`. Link someone to a palette with `?palette=riso`.

## Where the news comes from

Structured feeds and APIs only — nothing scrapes or diffs web pages.

| Desk | Examples |
| --- | --- |
| Labs | OpenAI, Anthropic, Google DeepMind, Gemini, Meta AI, Mistral, xAI, DeepSeek, Qwen, Microsoft, NVIDIA, Thinking Machines |
| Research | HF Daily Papers, Google Research, Apple ML, Microsoft Research, Ai2, Sakana, Epoch AI, BAIR |
| Analysis | Simon Willison, Import AI, Interconnects, Latent Space (AINews), Artificial Analysis, Ahead of AI, SemiAnalysis, Karpathy |
| News | The Decoder, The Verge, TechCrunch, Ars Technica, MIT Technology Review, Wired, IEEE Spectrum |
| Community | Hacker News (AI stories above a points bar), Lobsters |
| Open source | Hugging Face trending models & blog, GitHub releases, PyTorch, Ollama |

Labs without official RSS (Anthropic, Meta, Mistral, xAI, DeepSeek, Qwen)
are covered through maintained community-generated feeds. The full list,
with weights, lives in [`src/news_bot/sources.toml`](src/news_bot/sources.toml);
the **Sources** page on the site shows each one's live health.

## How it works

```
GitHub Actions (every 2h)
  fetch 60+ feeds/APIs concurrently ─► archive (data branch, one JSON file per day)
  ─► Groq: summary · "why it matters" · topic · importance 1-5 (batched, rate-limit aware)
  ─► cluster the same event across sources ─► rank
  ─► Groq: daily brief + weekly review
  ─► build static site ─► GitHub Pages
```

- `src/news_bot/fetchers.py` — RSS/Atom (feedparser), Hacker News Algolia,
  HF Daily Papers and HF trending adapters. A failing source is reported on
  the Sources page, never fatal.
- `src/news_bot/llm.py` — Groq client that rotates across models (each has
  its own free-tier token bucket), honours rate-limit headers, and detects
  retired model ids automatically.
- `src/news_bot/enrich.py` — prompts and validation for summaries and briefs.
- `src/news_bot/cluster.py` — merges coverage of one event (same URL, or
  matching rare names like "Gemini 4 Argon") into one story.
- `src/news_bot/store.py` — the archive, kept on the `data` branch as one
  squashed commit so the repo never grows.
- `site/` — the reader: plain HTML/CSS/JS, no build step.
- `content/` — the curated catch-up guide and conference calendar.

## Setup

Already done for this repository; for a fork:

1. **Groq key** — create a free key at console.groq.com and add it as the
   repository secret `GROQ_API_KEY`.
2. **Pages** — Settings → Pages → Source: *GitHub Actions*.
3. **Run** — Actions → *Collect & publish* → *Run workflow*. After that it
   runs every two hours by itself.

Optional repository variables: `GROQ_MODEL` (preferred model) and `NEWS_TZ`
(timezone for daily-brief dates, default `Asia/Kolkata`).

## Local use

```sh
uv sync --dev
cp .env.example .env               # add GROQ_API_KEY
set -a; . ./.env; set +a
uv run news-bot run                # fetch + summarise + build into _site/
uv run news-bot serve              # http://localhost:8000
uv run news-bot sources            # health check of every source
uv run news-bot build              # rebuild site from stored data, no network
uv run pytest && uv run ruff check src tests
```

To add a source, append a `[[source]]` block to `sources.toml` and run
`uv run news-bot sources <id>` to check it.
