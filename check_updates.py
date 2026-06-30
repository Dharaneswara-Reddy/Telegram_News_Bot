"""
AI release-notes watcher.

Checks the configured list of official AI product pages once per run,
detects new content since the last run (using a saved snapshot per page),
summarizes genuinely new content with Groq, and posts a formatted message
to Telegram.

Sources and snapshot state both live in Upstash Redis (see storage.py) —
this script holds no hardcoded source list anymore. Manage sources with
manage_sources.py.

Run this on a schedule (see workflow file).
"""

import os
import re
import hashlib
import difflib
import logging
import sys
from datetime import datetime, timezone

import requests

import storage

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("news-bot")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

GROQ_MODEL = "llama-3.3-70b-versatile"  # solid general-purpose Groq model; swap freely

# Sources now live in Upstash Redis (see storage.py) rather than being
# hardcoded here. storage.get_sources() returns the current list, seeded
# with sane defaults on first-ever run. Add/remove sources with
# manage_sources.py instead of editing this file.

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; personal-news-watcher/1.0; +https://github.com/)"
}

REQUEST_TIMEOUT = 20


# ---------------------------------------------------------------------------
# Fetch + text extraction
# ---------------------------------------------------------------------------

def fetch_page_text(url: str) -> str:
    """Fetch a URL and return a cleaned, plain-text version of its visible content.

    Deliberately simple (regex-strip tags) rather than pulling in a heavy HTML
    parser dependency. Good enough for diffing; not meant to be pixel-perfect.
    """
    resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    html = resp.text

    # Drop script/style blocks entirely, then strip remaining tags.
    html = re.sub(r"<(script|style)\b[^<]*(?:(?!</\1>)<[^<]*)*</\1>", " ", html, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Diffing
# ---------------------------------------------------------------------------

def extract_new_content(old_text: str, new_text: str, mode: str) -> str:
    """Return only the substantive new content, or '' if nothing meaningful changed.

    For rolling_log pages: find lines present in new_text but not old_text,
    via a line-level diff, and return them as a block. This correctly
    captures "one paragraph got added to a long changelog" instead of
    re-flagging the whole page.

    For article_list pages: same mechanism works fine too, since a new
    headline/snippet is just new lines that didn't exist before.
    """
    old_lines = [l.strip() for l in old_text.splitlines() if l.strip()]
    new_lines = [l.strip() for l in new_text.splitlines() if l.strip()]

    sm = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
    added = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag in ("insert", "replace"):
            added.extend(new_lines[j1:j2])

    # Filter out tiny/noise additions (timestamps, single short words, etc.)
    # — keep lines with reasonable substance only.
    added = [l for l in added if len(l) > 25]

    # De-dupe while preserving order.
    seen = set()
    deduped = []
    for l in added:
        if l not in seen:
            seen.add(l)
            deduped.append(l)

    combined = "\n".join(deduped)

    # Cap length sent downstream — a giant first-run diff (e.g. very first
    # time this source is ever checked) shouldn't blow the LLM context or
    # produce a useless mega-summary. Truncate with a note.
    MAX_CHARS = 6000
    if len(combined) > MAX_CHARS:
        combined = combined[:MAX_CHARS] + "\n[...truncated, content was longer...]"

    return combined


# ---------------------------------------------------------------------------
# Groq summarization
# ---------------------------------------------------------------------------

SUMMARY_PROMPT_TEMPLATE = """You are summarizing a fragment of new content that just appeared on an official AI company update/release-notes page. The fragment may be terse or list-like (it's scraped from a changelog or news page), not polished prose.

Company/product: {label}
Source URL: {url}

New content fragment:
---
{fragment}
---

Write a response with exactly two parts, each 1-2 sentences, plain text, no markdown formatting, no bullet points:

EXPLANATION: A small, plain explanation of what this update/announcement actually is, in your own words. Do not just restate the fragment.

USE_CASE: How and where an average software developer (general audience, not a specific company or stack) could realistically use or benefit from this, if at all. If this update genuinely isn't relevant or useful to an average developer (e.g. it's a minor UI tweak, pricing change in a region, or enterprise-only admin feature), say so plainly instead of inventing a use case — do not force relevance that isn't there.

Respond in exactly this format with no other text:
EXPLANATION: <text>
USE_CASE: <text>
"""


def summarize_with_groq(label: str, url: str, fragment: str) -> dict | None:
    if not fragment.strip():
        return None

    prompt = SUMMARY_PROMPT_TEMPLATE.format(label=label, url=url, fragment=fragment)

    try:
        resp = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": GROQ_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3,
                "max_tokens": 300,
            },
            timeout=30,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        log.error("Groq call failed for %s: %s", label, e)
        return None

    explanation_match = re.search(r"EXPLANATION:\s*(.+?)(?=\nUSE_CASE:|\Z)", content, re.DOTALL)
    use_case_match = re.search(r"USE_CASE:\s*(.+)", content, re.DOTALL)

    explanation = explanation_match.group(1).strip() if explanation_match else None
    use_case = use_case_match.group(1).strip() if use_case_match else None

    if not explanation or not use_case:
        log.warning("Groq response for %s didn't match expected format: %r", label, content)
        return None

    return {"explanation": explanation, "use_case": use_case}


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def send_telegram_message(text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("Telegram credentials missing — printing message instead:\n%s", text)
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(
            url,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": False,
            },
            timeout=15,
        )
        resp.raise_for_status()
        return True
    except Exception as e:
        log.error("Telegram send failed: %s", e)
        return False


def format_message(label: str, url: str, summary: dict, is_first_check: bool) -> str:
    note = "\n<i>(first check for this source — establishing baseline, treat as informational)</i>" if is_first_check else ""
    return (
        f"<b>Who posted:</b> {label}\n"
        f"<b>Explanation:</b> {summary['explanation']}\n"
        f"<b>How/where to use it:</b> {summary['use_case']}\n"
        f"<b>Link:</b> {url}"
        f"{note}"
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    missing = [name for name, val in [
        ("GROQ_API_KEY", GROQ_API_KEY),
        ("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN),
        ("TELEGRAM_CHAT_ID", TELEGRAM_CHAT_ID),
        ("UPSTASH_REDIS_REST_URL", os.environ.get("UPSTASH_REDIS_REST_URL")),
        ("UPSTASH_REDIS_REST_TOKEN", os.environ.get("UPSTASH_REDIS_REST_TOKEN")),
    ] if not val]
    if missing:
        log.error("Missing required environment variables: %s", ", ".join(missing))
        sys.exit(1)

    try:
        sources = storage.get_sources()
    except Exception as e:
        log.error("Could not load sources from storage: %s", e)
        sys.exit(1)

    log.info("Checking %d source(s).", len(sources))
    run_time = datetime.now(timezone.utc).isoformat()
    any_failures = False

    for source in sources:
        sid, label, url, mode = source["id"], source["label"], source["url"], source["mode"]
        log.info("Checking %s (%s)", label, url)

        try:
            new_text = fetch_page_text(url)
        except Exception as e:
            log.error("Fetch failed for %s: %s", label, e)
            any_failures = True
            continue

        new_hash = text_hash(new_text)

        try:
            prior = storage.get_snapshot(sid)
        except Exception as e:
            log.error("Could not read snapshot for %s from storage: %s — skipping this run.", label, e)
            any_failures = True
            continue

        if prior is None:
            # First time ever checking this source — save baseline, don't
            # spam a summary of the entire page on day one.
            log.info("First-ever check for %s — saving baseline, no message sent.", label)
            storage.set_snapshot(sid, {"hash": new_hash, "text": new_text, "last_checked": run_time})
            continue

        if prior["hash"] == new_hash:
            log.info("No change for %s.", label)
            prior["last_checked"] = run_time
            storage.set_snapshot(sid, prior)
            continue

        log.info("Change detected for %s — extracting new content.", label)
        fragment = extract_new_content(prior["text"], new_text, mode)

        if not fragment.strip():
            # Hash changed but nothing substantive diffed out (e.g. a
            # timestamp-only change, or removed content). Update snapshot,
            # stay quiet.
            log.info("Change for %s was not substantive enough to report.", label)
            storage.set_snapshot(sid, {"hash": new_hash, "text": new_text, "last_checked": run_time})
            continue

        summary = summarize_with_groq(label, url, fragment)
        if summary is None:
            log.warning("Could not summarize change for %s — skipping message, will retry next run if still new.", label)
            any_failures = True
            # Deliberately do NOT update the snapshot here, so the same
            # fragment gets retried next hour rather than silently lost.
            continue

        message = format_message(label, url, summary, is_first_check=False)
        sent = send_telegram_message(message)
        if not sent:
            any_failures = True
            continue  # don't update snapshot, retry next run

        storage.set_snapshot(sid, {"hash": new_hash, "text": new_text, "last_checked": run_time})

    log.info("Run complete.")

    if any_failures:
        sys.exit(2)  # non-zero exit so the GH Action run is visibly flagged, without erroring the whole workflow fatally


if __name__ == "__main__":
    main()