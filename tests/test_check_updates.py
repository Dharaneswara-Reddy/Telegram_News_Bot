"""Tests for news_bot.check_updates module."""

import hashlib
from unittest.mock import MagicMock, patch

import pytest

from news_bot.check_updates import (
    extract_new_content,
    fetch_page_text,
    format_message,
    send_telegram_message,
    summarize_with_groq,
    text_hash,
)

# ---------------------------------------------------------------------------
# fetch_page_text
# ---------------------------------------------------------------------------


class TestFetchPageText:
    """Tests for HTML fetching and text extraction."""

    def test_strips_html_tags(self):
        html = "<html><body><p>Hello world</p></body></html>"
        mock_resp = MagicMock()
        mock_resp.text = html
        mock_resp.raise_for_status = MagicMock()

        with patch("news_bot.check_updates.requests.get", return_value=mock_resp):
            result = fetch_page_text("https://example.com")

        assert "Hello world" in result
        assert "<p>" not in result
        assert "<html>" not in result

    def test_strips_script_and_style_blocks(self):
        html = """
        <html>
        <head><style>body { color: red; }</style></head>
        <body>
            <script>alert('hi');</script>
            <p>Visible content</p>
        </body>
        </html>
        """
        mock_resp = MagicMock()
        mock_resp.text = html
        mock_resp.raise_for_status = MagicMock()

        with patch("news_bot.check_updates.requests.get", return_value=mock_resp):
            result = fetch_page_text("https://example.com")

        assert "Visible content" in result
        assert "alert" not in result
        assert "color: red" not in result

    def test_decodes_html_entities(self):
        html = "<p>Tom&amp;Jerry&nbsp;show</p>"
        mock_resp = MagicMock()
        mock_resp.text = html
        mock_resp.raise_for_status = MagicMock()

        with patch("news_bot.check_updates.requests.get", return_value=mock_resp):
            result = fetch_page_text("https://example.com")

        assert "Tom&Jerry" in result

    def test_raises_on_http_error(self):
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = Exception("404 Not Found")

        with (
            patch("news_bot.check_updates.requests.get", return_value=mock_resp),
            pytest.raises(Exception, match="404 Not Found"),
        ):
            fetch_page_text("https://example.com/missing")


# ---------------------------------------------------------------------------
# text_hash
# ---------------------------------------------------------------------------


class TestTextHash:
    """Tests for SHA-256 text hashing."""

    def test_returns_sha256_hex(self):
        text = "hello world"
        expected = hashlib.sha256(text.encode("utf-8")).hexdigest()
        assert text_hash(text) == expected

    def test_different_text_different_hash(self):
        assert text_hash("aaa") != text_hash("bbb")

    def test_same_text_same_hash(self):
        assert text_hash("same") == text_hash("same")


# ---------------------------------------------------------------------------
# extract_new_content
# ---------------------------------------------------------------------------


class TestExtractNewContent:
    """Tests for content diffing logic."""

    def test_detects_added_lines(self):
        old = "Line one\nLine two\nLine three"
        new = "Line one\nLine two\nThis is a brand new line that was just added to the page\nLine three"
        result = extract_new_content(old, new, "rolling_log")
        assert "brand new line" in result

    def test_filters_short_noise_lines(self):
        old = "Existing content here on the page"
        new = "Existing content here on the page\nhi\nok\n12:34"
        result = extract_new_content(old, new, "rolling_log")
        # All additions are ≤25 chars, should be filtered
        assert result.strip() == ""

    def test_deduplicates_added_lines(self):
        old = "Line one"
        new = (
            "Line one\n"
            "This is a duplicated line that appears twice in the new content\n"
            "This is a duplicated line that appears twice in the new content"
        )
        result = extract_new_content(old, new, "article_list")
        count = result.count("This is a duplicated line that appears twice in the new content")
        assert count == 1

    def test_truncates_long_content(self):
        old = "Start"
        # Create content that exceeds 6000 chars
        long_line = "A" * 100 + " this is a substantive line with enough length"
        new = "Start\n" + "\n".join(f"{long_line} {i}" for i in range(100))
        result = extract_new_content(old, new, "rolling_log")
        assert "[...truncated, content was longer...]" in result

    def test_no_change_returns_empty(self):
        text = "Same content\nOn multiple lines"
        result = extract_new_content(text, text, "rolling_log")
        assert result.strip() == ""

    def test_works_for_article_list_mode(self):
        old = "Article A headline and its full description here"
        new = "Article A headline and its full description here\nArticle B headline a brand new article just posted"
        result = extract_new_content(old, new, "article_list")
        assert "Article B" in result


# ---------------------------------------------------------------------------
# summarize_with_groq
# ---------------------------------------------------------------------------


class TestSummarizeWithGroq:
    """Tests for Groq LLM summarization."""

    def test_parses_valid_response(self):
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "choices": [
                {
                    "message": {
                        "content": "EXPLANATION: This is an update.\nUSE_CASE: Developers can use this."
                    }
                }
            ]
        }

        with patch("news_bot.check_updates.requests.post", return_value=mock_resp):
            result = summarize_with_groq("TestCo", "https://example.com", "New feature X released")

        assert result is not None
        assert result["explanation"] == "This is an update."
        assert result["use_case"] == "Developers can use this."

    def test_returns_none_on_empty_fragment(self):
        result = summarize_with_groq("TestCo", "https://example.com", "   ")
        assert result is None

    def test_returns_none_on_api_error(self):
        with patch(
            "news_bot.check_updates.requests.post",
            side_effect=Exception("Connection timeout"),
        ):
            result = summarize_with_groq("TestCo", "https://example.com", "Some content")

        assert result is None

    def test_returns_none_on_malformed_response(self):
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": "Just some random text with no format"}}]
        }

        with patch("news_bot.check_updates.requests.post", return_value=mock_resp):
            result = summarize_with_groq("TestCo", "https://example.com", "New feature")

        assert result is None


# ---------------------------------------------------------------------------
# send_telegram_message
# ---------------------------------------------------------------------------


class TestSendTelegramMessage:
    """Tests for Telegram message sending."""

    @patch.dict(
        "os.environ",
        {"TELEGRAM_BOT_TOKEN": "fake-token", "TELEGRAM_CHAT_ID": "12345"},
    )
    def test_sends_successfully(self):
        # Re-import to pick up patched env vars
        import news_bot.check_updates as mod

        original_token = mod.TELEGRAM_BOT_TOKEN
        original_chat = mod.TELEGRAM_CHAT_ID
        mod.TELEGRAM_BOT_TOKEN = "fake-token"
        mod.TELEGRAM_CHAT_ID = "12345"

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()

        try:
            with patch("news_bot.check_updates.requests.post", return_value=mock_resp):
                result = send_telegram_message("Test message")
            assert result is True
        finally:
            mod.TELEGRAM_BOT_TOKEN = original_token
            mod.TELEGRAM_CHAT_ID = original_chat

    def test_returns_false_when_credentials_missing(self):
        import news_bot.check_updates as mod

        original_token = mod.TELEGRAM_BOT_TOKEN
        original_chat = mod.TELEGRAM_CHAT_ID
        mod.TELEGRAM_BOT_TOKEN = None
        mod.TELEGRAM_CHAT_ID = None

        try:
            result = send_telegram_message("Test message")
            assert result is False
        finally:
            mod.TELEGRAM_BOT_TOKEN = original_token
            mod.TELEGRAM_CHAT_ID = original_chat

    def test_returns_false_on_api_error(self):
        import news_bot.check_updates as mod

        original_token = mod.TELEGRAM_BOT_TOKEN
        original_chat = mod.TELEGRAM_CHAT_ID
        mod.TELEGRAM_BOT_TOKEN = "fake-token"
        mod.TELEGRAM_CHAT_ID = "12345"

        try:
            with patch(
                "news_bot.check_updates.requests.post",
                side_effect=Exception("Network error"),
            ):
                result = send_telegram_message("Test message")
            assert result is False
        finally:
            mod.TELEGRAM_BOT_TOKEN = original_token
            mod.TELEGRAM_CHAT_ID = original_chat


# ---------------------------------------------------------------------------
# format_message
# ---------------------------------------------------------------------------


class TestFormatMessage:
    """Tests for Telegram message formatting."""

    def test_standard_message_format(self):
        summary = {
            "explanation": "New model released.",
            "use_case": "Use it for coding tasks.",
        }
        result = format_message("OpenAI", "https://openai.com", summary, is_first_check=False)

        assert "<b>Who posted:</b> OpenAI" in result
        assert "<b>Explanation:</b> New model released." in result
        assert "<b>How/where to use it:</b> Use it for coding tasks." in result
        assert "<b>Link:</b> https://openai.com" in result
        assert "first check" not in result

    def test_first_check_includes_note(self):
        summary = {
            "explanation": "Something.",
            "use_case": "Something.",
        }
        result = format_message("Anthropic", "https://anthropic.com", summary, is_first_check=True)

        assert "first check for this source" in result
        assert "establishing baseline" in result
