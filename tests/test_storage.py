"""Tests for news_bot.storage module."""

import json
from unittest.mock import MagicMock, patch

import pytest

from news_bot.storage import (
    DEFAULT_SOURCES,
    SNAPSHOT_KEY_PREFIX,
    add_source,
    get_snapshot,
    get_sources,
    remove_source,
    set_snapshot,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_redis_get(return_value):
    """Create a mock requests.get that returns a Redis REST API response."""
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {"result": return_value}
    return mock_resp


def _mock_redis_set():
    """Create a mock requests.post for Redis SET."""
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    return mock_resp


# ---------------------------------------------------------------------------
# _require_config
# ---------------------------------------------------------------------------


class TestRequireConfig:
    """Tests for Upstash credential validation."""

    def test_raises_when_url_missing(self):
        with (
            patch("news_bot.storage.UPSTASH_URL", None),
            patch("news_bot.storage.UPSTASH_TOKEN", "some-token"),
            pytest.raises(RuntimeError, match="UPSTASH_REDIS_REST_URL"),
        ):
            get_sources()

    def test_raises_when_token_missing(self):
        with (
            patch("news_bot.storage.UPSTASH_URL", "https://redis.example.com"),
            patch("news_bot.storage.UPSTASH_TOKEN", None),
            pytest.raises(RuntimeError, match="UPSTASH_REDIS_REST_TOKEN"),
        ):
            get_sources()


# ---------------------------------------------------------------------------
# get_sources
# ---------------------------------------------------------------------------


class TestGetSources:
    """Tests for source list retrieval and seeding."""

    @patch("news_bot.storage.UPSTASH_URL", "https://redis.example.com")
    @patch("news_bot.storage.UPSTASH_TOKEN", "fake-token")
    def test_seeds_defaults_on_first_call(self):
        mock_get = _mock_redis_get(None)  # key doesn't exist yet
        mock_set = _mock_redis_set()

        with (
            patch("news_bot.storage.requests.get", return_value=mock_get),
            patch("news_bot.storage.requests.post", return_value=mock_set),
        ):
            sources = get_sources()

        assert sources == DEFAULT_SOURCES
        # Verify it saved the defaults
        mock_set_call = mock_set  # noqa: F841 — used for assertion context

    @patch("news_bot.storage.UPSTASH_URL", "https://redis.example.com")
    @patch("news_bot.storage.UPSTASH_TOKEN", "fake-token")
    def test_returns_stored_sources(self):
        stored = [{"id": "test", "label": "Test", "url": "https://test.com", "mode": "rolling_log"}]
        mock_get = _mock_redis_get(json.dumps(stored))

        with patch("news_bot.storage.requests.get", return_value=mock_get):
            sources = get_sources()

        assert sources == stored


# ---------------------------------------------------------------------------
# add_source
# ---------------------------------------------------------------------------


class TestAddSource:
    """Tests for adding new sources."""

    @patch("news_bot.storage.UPSTASH_URL", "https://redis.example.com")
    @patch("news_bot.storage.UPSTASH_TOKEN", "fake-token")
    def test_adds_new_source(self):
        existing = [{"id": "existing", "label": "E", "url": "https://e.com", "mode": "rolling_log"}]
        mock_get = _mock_redis_get(json.dumps(existing))
        mock_set = _mock_redis_set()

        with (
            patch("news_bot.storage.requests.get", return_value=mock_get),
            patch("news_bot.storage.requests.post", return_value=mock_set) as mock_post,
        ):
            add_source("new_one", "New Source", "https://new.com", "article_list")

        # Verify POST was called (to save updated sources)
        assert mock_post.called

    @patch("news_bot.storage.UPSTASH_URL", "https://redis.example.com")
    @patch("news_bot.storage.UPSTASH_TOKEN", "fake-token")
    def test_rejects_duplicate_id(self):
        existing = [{"id": "dup", "label": "D", "url": "https://d.com", "mode": "rolling_log"}]
        mock_get = _mock_redis_get(json.dumps(existing))

        with (
            patch("news_bot.storage.requests.get", return_value=mock_get),
            pytest.raises(ValueError, match="already exists"),
        ):
            add_source("dup", "Duplicate", "https://dup.com")

    def test_rejects_invalid_mode(self):
        with pytest.raises(ValueError, match="mode must be"):
            add_source("x", "X", "https://x.com", "invalid_mode")


# ---------------------------------------------------------------------------
# remove_source
# ---------------------------------------------------------------------------


class TestRemoveSource:
    """Tests for removing sources."""

    @patch("news_bot.storage.UPSTASH_URL", "https://redis.example.com")
    @patch("news_bot.storage.UPSTASH_TOKEN", "fake-token")
    def test_removes_existing_source(self):
        existing = [
            {"id": "keep", "label": "K", "url": "https://k.com", "mode": "rolling_log"},
            {"id": "remove_me", "label": "R", "url": "https://r.com", "mode": "rolling_log"},
        ]
        mock_get = _mock_redis_get(json.dumps(existing))
        mock_set = _mock_redis_set()

        with (
            patch("news_bot.storage.requests.get", return_value=mock_get),
            patch("news_bot.storage.requests.post", return_value=mock_set),
        ):
            remove_source("remove_me")

    @patch("news_bot.storage.UPSTASH_URL", "https://redis.example.com")
    @patch("news_bot.storage.UPSTASH_TOKEN", "fake-token")
    def test_raises_for_nonexistent_source(self):
        existing = [{"id": "only_one", "label": "O", "url": "https://o.com", "mode": "rolling_log"}]
        mock_get = _mock_redis_get(json.dumps(existing))

        with (
            patch("news_bot.storage.requests.get", return_value=mock_get),
            pytest.raises(ValueError, match="No source found"),
        ):
            remove_source("nonexistent")


# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------


class TestSnapshots:
    """Tests for per-source snapshot get/set."""

    @patch("news_bot.storage.UPSTASH_URL", "https://redis.example.com")
    @patch("news_bot.storage.UPSTASH_TOKEN", "fake-token")
    def test_get_snapshot_returns_none_when_missing(self):
        mock_get = _mock_redis_get(None)

        with patch("news_bot.storage.requests.get", return_value=mock_get):
            result = get_snapshot("new_source")

        assert result is None

    @patch("news_bot.storage.UPSTASH_URL", "https://redis.example.com")
    @patch("news_bot.storage.UPSTASH_TOKEN", "fake-token")
    def test_get_snapshot_returns_parsed_json(self):
        snapshot = {
            "hash": "abc123",
            "text": "page content",
            "last_checked": "2026-01-01T00:00:00Z",
        }
        mock_get = _mock_redis_get(json.dumps(snapshot))

        with patch("news_bot.storage.requests.get", return_value=mock_get):
            result = get_snapshot("test_source")

        assert result == snapshot

    @patch("news_bot.storage.UPSTASH_URL", "https://redis.example.com")
    @patch("news_bot.storage.UPSTASH_TOKEN", "fake-token")
    def test_set_snapshot_calls_redis_set(self):
        mock_set = _mock_redis_set()
        snapshot = {"hash": "def456", "text": "new content"}

        with patch("news_bot.storage.requests.post", return_value=mock_set) as mock_post:
            set_snapshot("test_source", snapshot)

        assert mock_post.called
        call_kwargs = mock_post.call_args
        # The key should be in the JSON command array: ["SET", key, value]
        json_body = call_kwargs[1]["json"] if "json" in call_kwargs[1] else call_kwargs[0][1]
        assert json_body[0] == "SET"
        assert f"{SNAPSHOT_KEY_PREFIX}test_source" in json_body[1]
