# AI release-notes watcher

Checks a configurable list of official AI product pages (Anthropic, OpenAI,
Google/Gemini, Microsoft Copilot by default — add your own anytime) once an
hour, and sends a Telegram message for anything genuinely new, summarized
by Groq.

## What it does each run

1. Reads the current source list from Upstash Redis (seeded with 11
   defaults the first time it's ever run).
2. Fetches each page and compares it against the last snapshot saved for
   that source (also in Upstash).
3. If new lines appear, extracts just the new fragment (not the whole
   page).
4. Sends that fragment to Groq, asking for a small explanation and a
   "how/where an average developer could use this" note.
5. Posts the result to your Telegram chat in the four-field format.
6. Saves the new snapshot so next hour's check only flags what's new
   *since this run*.

The first time it ever checks a given source, it saves a baseline silently
— it won't dump a "summary" of an entire existing page on day one, since
that's not actually new information.

## Architecture

- `check_updates.py` — the main script, run on a schedule. No hardcoded
  source list anymore; pulls sources from storage at runtime.
- `storage.py` — thin wrapper around Upstash Redis's REST API. Holds the
  sources list (one Redis key, a JSON list) and one snapshot per source
  (one Redis key each). Plain HTTPS calls, no persistent connection or
  driver — suits a script that starts cold every hour.
- `manage_sources.py` — small CLI for adding/removing/listing sources
  without touching `check_updates.py` or redeploying anything.

State lives entirely in Upstash now — nothing is written back to the repo,
so the GitHub Action doesn't need write permissions and there's no commit
history to manage.

## One-time setup

### 1. Make a Telegram bot and get your chat ID

- Open Telegram, message **@BotFather**, send `/newbot`, follow the prompts.
  You'll get a token that looks like `123456789:AAH...` — that's
  `TELEGRAM_BOT_TOKEN`.
- Send your new bot any message (e.g. "hi") so it has a conversation to
  reply into.
- Visit `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser
  (replace `<YOUR_TOKEN>`). Look for `"chat":{"id": ...}` in the response —
  that number is your `TELEGRAM_CHAT_ID`.

### 2. Get a Groq API key

- Sign up at console.groq.com, create an API key. That's `GROQ_API_KEY`.
- Free tier is plenty for ~11 pages checked hourly — this is a tiny amount
  of token volume per day.

### 3. Set up Upstash Redis (free tier)

- Sign up at upstash.com (no credit card needed for the free tier).
- Create a new Redis database. Any region is fine — this isn't
  latency-sensitive.
- On the database's detail page, find the **REST API** section. You need
  two values: `UPSTASH_REDIS_REST_URL` and `UPSTASH_REDIS_REST_TOKEN`
  (sometimes shown as just "URL" and "Token" — they're right next to each
  other, copy both).
- Free tier covers this comfortably: 11 sources checked hourly is roughly
  22 Redis commands/hour (one read + one write per source), nowhere close
  to free-tier daily limits.

### 4. Put this in a GitHub repo

- Create a new repo (can be private), push this `news-bot/` folder into it
  (including the `.github/workflows/watch.yml` file — that path matters,
  GitHub only picks up workflows from exactly `.github/workflows/`).
- Go to **Settings → Secrets and variables → Actions** in the repo, and add
  five repository secrets: `GROQ_API_KEY`, `TELEGRAM_BOT_TOKEN`,
  `TELEGRAM_CHAT_ID`, `UPSTASH_REDIS_REST_URL`, `UPSTASH_REDIS_REST_TOKEN`,
  using the values from steps 1–3.
- Go to the **Actions** tab, find "AI release-notes watcher," and click
  **Run workflow** to trigger it manually once — this both tests that
  everything's wired correctly and establishes the baseline snapshot for
  every source (so you won't get an 11-message flood the first real hour).
- After that first manual run, it'll run automatically every hour via the
  cron schedule. No further action needed.

## Adding or removing sources

This is the part that used to require editing Python — now it doesn't.
From any machine with Python and the same Upstash credentials set as
environment variables:

```bash
# See what's currently being watched
python manage_sources.py list

# Add a new source — id should be short/stable/no-spaces, label is what
# shows in the "Who posted" field, mode is rolling_log (changelog-style,
# one accumulating page) or article_list (news/blog index with discrete
# posts) — both currently behave the same, the distinction just documents
# intent for future tuning
python manage_sources.py add mistral_news "Mistral AI" https://mistral.ai/news/ article_list

# Remove one
python manage_sources.py remove mistral_news
```

A newly added source gets the same silent-baseline treatment as the
original 11 did — its first check just establishes a starting point, no
message is sent for "everything currently on the page," only for what's
new after that.

If you'd rather not run this from your own machine, you can also trigger
it via a one-off `workflow_dispatch` style manual Action run — but a
script with an `add`/`remove` command isn't something a scheduled trigger
maps onto cleanly, so running it locally (or wherever you have Python) is
the more natural fit here.

## A few things worth knowing going in

- **The first run for each source is silent by design.** This applies to
  the original 11 *and* anything you add later via `manage_sources.py`.
- **Some sources will rarely fire.** Pages like the OpenAI/Anthropic news
  pages update in bursts (a model launch, then quiet for days). Pages like
  changelogs update more often but sometimes in small increments. This is
  expected — an hourly check on a page that updates twice a week will
  mostly say "no change."
- **If a Groq or Telegram call fails mid-run, that source's snapshot is
  deliberately *not* updated**, so the same new content gets retried on the
  next run instead of silently disappearing. You may occasionally see the
  same item attempted twice if e.g. Telegram's API hiccups right after Groq
  succeeded — rare, but worth knowing the failure mode is "retry" not
  "silent gap."
- **The page-scraping is intentionally simple** (regex tag-stripping, not a
  full HTML/JS-rendering browser). The default 11 sources are all
  server-rendered pages, so this works, but if you add a source that's a
  heavy JavaScript-rendered single-page app, this approach won't see its
  content — that would need a headless browser instead, which is a bigger
  dependency than this project currently carries.
- **Diff quality on `rolling_log` pages depends on line-level changes being
  genuinely new lines**, not reformatted versions of old lines (e.g. if a
  site re-renders its whole changelog with different whitespace/wrapping
  on every request, the diff could over-trigger). If you notice a specific
  source behaving oddly after a week of real use, that's the first place
  to look — not a sign the whole approach is broken.
- **Cost**: GitHub Actions free tier covers way more than 24 runs/day for a
  public repo, and a comfortable amount even for a private repo on the
  free plan. Groq's free tier covers this volume easily. Upstash's free
  tier covers it easily too. Telegram is free. This should cost you
  nothing to run as-is, even as you add a handful more sources.
- **This watches official sources only, by design.** It does not search
  Reddit, Medium, Hacker News, or general web search — those surfaces
  involve people reacting to news, not the news itself, and mixing
  "company announced X" with "someone's blog post about X" in the same
  feed makes the feed harder to trust, not more useful. If you want to
  track something outside official channels later, the cleanest way is
  still adding it as its own labeled source via `manage_sources.py` — the
  bot will just treat it as one more page to diff, same as everything
  else.