"""Tests for news_bot.textutil."""

from news_bot.textutil import canonical_url, html_to_text, parse_iso, title_tokens


class TestCanonicalUrl:
    def test_strips_tracking_params_www_and_trailing_slash(self):
        a = canonical_url("https://www.example.com/post/?utm_source=x&ref=hn")
        b = canonical_url("http://example.com/post")
        assert a == b == "https://example.com/post"

    def test_keeps_meaningful_query(self):
        assert canonical_url("https://x.com/a?id=5") == "https://x.com/a?id=5"

    def test_arxiv_and_hf_papers_collapse_to_one_key(self):
        keys = {
            canonical_url("https://arxiv.org/abs/2609.38792v2"),
            canonical_url("https://arxiv.org/pdf/2609.38792"),
            canonical_url("https://huggingface.co/papers/2609.38792"),
        }
        assert keys == {"arxiv:2609.38792"}

    def test_meaningful_fragment_kept_noise_fragment_dropped(self):
        a = canonical_url("https://api-docs.deepseek.com/updates/#v4-release")
        b = canonical_url("https://api-docs.deepseek.com/updates/#v41-release")
        assert a != b
        assert canonical_url("https://blog.example.com/p#comments") == "https://blog.example.com/p"


class TestHtmlToText:
    def test_strips_tags_scripts_and_entities(self):
        raw = "<p>Hello&nbsp;<b>world</b> &amp; co</p><script>evil()</script>"
        assert html_to_text(raw) == "Hello world & co"

    def test_truncates_on_word_boundary(self):
        out = html_to_text("alpha beta gamma delta", limit=12)
        assert out.endswith("…")
        assert len(out) <= 13

    def test_empty(self):
        assert html_to_text(None) == ""


def test_title_tokens_keeps_version_digits_and_drops_stopwords():
    toks = title_tokens("Google releases Gemini 4 Argon, the new model")
    assert {"gemini", "4", "argon", "google"} <= toks
    assert "the" not in toks


def test_parse_iso_handles_z_and_naive():
    assert parse_iso("2026-10-02T10:00:00Z").utcoffset().total_seconds() == 0
    assert parse_iso("2026-10-02T10:00:00").tzinfo is not None
    assert parse_iso("not a date") is None
