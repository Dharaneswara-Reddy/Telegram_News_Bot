---
type: Integration
title: Groq Summarization
description: LLM-powered content summarization using a structured prompt template that extracts a plain-English explanation and developer use-case from raw page diffs.
tags: [groq, llm, summarization, ai, prompt-engineering]
timestamp: 2026-06-30T00:00:00Z
---

# Purpose

The Groq integration turns raw page diff fragments into human-readable summaries. It's called by the [watcher script](check_updates.md) after new content is extracted from a source page.

# Model

Uses `llama-3.3-70b-versatile` via the Groq API (`/openai/v1/chat/completions` endpoint). The model choice is configurable by changing the `GROQ_MODEL` constant. Temperature is set to 0.3 for consistent, factual output. Max tokens: 300.

# Prompt Design

The prompt template (`SUMMARY_PROMPT_TEMPLATE`) asks the LLM for exactly two parts:

1. **EXPLANATION** — A 1-2 sentence plain explanation of the update in the LLM's own words (not a restatement).
2. **USE_CASE** — How an average developer could use or benefit from this. The prompt explicitly instructs the LLM to say "not relevant" rather than inventing forced use cases.

The response must match the format:
```
EXPLANATION: <text>
USE_CASE: <text>
```

# Function

## `summarize_with_groq(label, url, fragment) → dict | None`

1. Returns `None` for empty/whitespace-only fragments.
2. Formats the prompt with the source label, URL, and fragment.
3. POSTs to Groq's API with Bearer auth.
4. Parses the response using regex to extract `EXPLANATION:` and `USE_CASE:` fields.
5. Returns `{"explanation": ..., "use_case": ...}` on success.
6. Returns `None` on API error or malformed response (logged as warning).

# Error Handling

- **API failure** (timeout, rate limit, 5xx): Caught, logged, returns `None`. The [watcher](check_updates.md) will retry next run since the snapshot is not updated.
- **Malformed response** (LLM didn't follow format): Logged as warning, returns `None`.

# Cost

Free tier is sufficient. At ~11 sources checked hourly, the daily token volume is minimal (each call uses <400 tokens total).
