---
type: Integration
title: Telegram Integration
description: Bot API integration for posting formatted HTML update messages to a configured Telegram chat.
tags: [telegram, bot-api, notifications, messaging]
timestamp: 2026-06-30T00:00:00Z
---

# Purpose

The Telegram integration is the notification delivery mechanism for the [watcher](check_updates.md). When new content is detected and [summarized](groq.md), it gets posted to a Telegram chat as a formatted HTML message.

# Configuration

Two environment variables:
- `TELEGRAM_BOT_TOKEN` — obtained from @BotFather via the `/newbot` command.
- `TELEGRAM_CHAT_ID` — the numeric chat ID where messages are sent. Obtained by messaging the bot and checking `/getUpdates`.

# Message Format

Messages use Telegram's HTML parse mode with four fields:

```
Who posted: <label>
Explanation: <from Groq summary>
How/where to use it: <from Groq summary>
Link: <source URL>
```

First-check messages append an italic note: *(first check for this source — establishing baseline, treat as informational)*.

# Functions

## `send_telegram_message(text) → bool`

POSTs to `https://api.telegram.org/bot<token>/sendMessage` with HTML parse mode and web preview enabled. Returns `True` on success, `False` on any error (logged, not raised).

If credentials are missing, the message is printed to stdout instead.

## `format_message(label, url, summary, is_first_check) → str`

Constructs the HTML message string from the source metadata and [Groq summary](groq.md) output.

# Error Handling

Telegram API failures are caught and logged. The [watcher](check_updates.md) deliberately does NOT update the source snapshot on send failure, so the same content gets retried on the next run.
