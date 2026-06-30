# AI Release-Notes Watcher — Knowledge Bundle

* [System Architecture](architecture.md) — Hourly fetch → diff → summarize → notify pipeline running statelessly on GitHub Actions with Upstash Redis for persistence
* [Watcher Script](check_updates.md) — Main scheduled script that orchestrates page fetching, content diffing, Groq summarization, and Telegram delivery
* [Storage Layer](storage.md) — Thin Upstash Redis REST wrapper managing the sources list and per-source snapshot state
* [Source Management CLI](manage_sources.md) — Command-line tool for adding, removing, and listing watched sources without touching code
* [Telegram Integration](telegram.md) — Bot API integration for posting formatted HTML update messages to a configured chat
* [Groq Summarization](groq.md) — LLM-powered content summarization using a structured prompt template with explanation and use-case extraction
