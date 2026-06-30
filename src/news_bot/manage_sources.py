"""
Manage the watched sources list.

Usage:
    python -m news_bot.manage_sources list
    python -m news_bot.manage_sources add <id> "<label>" <url> [rolling_log|article_list]
    python -m news_bot.manage_sources remove <id>

Requires UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN to be set in
the environment (same credentials check_updates.py uses).

Examples:
    python -m news_bot.manage_sources add mistral_news "Mistral AI" https://mistral.ai/news article_list
    python -m news_bot.manage_sources remove mistral_news
    python -m news_bot.manage_sources list
"""

import logging
import sys

from news_bot import storage

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("manage-sources")


def cmd_list():
    sources = storage.get_sources()
    if not sources:
        print("No sources configured.")
        return
    print(f"{len(sources)} source(s):\n")
    for s in sources:
        print(f"  id:    {s['id']}")
        print(f"  label: {s['label']}")
        print(f"  url:   {s['url']}")
        print(f"  mode:  {s['mode']}")
        print()


def cmd_add(args):
    if len(args) < 3:
        print(
            'Usage: python -m news_bot.manage_sources add <id> "<label>" <url> [rolling_log|article_list]'
        )
        sys.exit(1)
    source_id, label, url = args[0], args[1], args[2]
    mode = args[3] if len(args) > 3 else "rolling_log"
    try:
        storage.add_source(source_id, label, url, mode)
        print(f"Added '{label}' ({url}) with id '{source_id}', mode '{mode}'.")
        print(
            "It'll be checked starting next scheduled run. First check establishes a silent baseline, as usual."
        )
    except ValueError as e:
        print(f"Error: {e}")
        sys.exit(1)


def cmd_remove(args):
    if len(args) < 1:
        print("Usage: python -m news_bot.manage_sources remove <id>")
        sys.exit(1)
    source_id = args[0]
    try:
        storage.remove_source(source_id)
        print(f"Removed source '{source_id}' and its saved snapshot.")
    except ValueError as e:
        print(f"Error: {e}")
        sys.exit(1)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    command = sys.argv[1]
    rest = sys.argv[2:]

    if command == "list":
        cmd_list()
    elif command == "add":
        cmd_add(rest)
    elif command == "remove":
        cmd_remove(rest)
    else:
        print(f"Unknown command: {command}\n")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
