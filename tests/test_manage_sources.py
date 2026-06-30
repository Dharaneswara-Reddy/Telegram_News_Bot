"""Tests for news_bot.manage_sources CLI module."""

from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# cmd_list
# ---------------------------------------------------------------------------


class TestCmdList:
    """Tests for the 'list' command."""

    def test_prints_sources(self, capsys):
        from news_bot.manage_sources import cmd_list

        mock_sources = [
            {
                "id": "test_src",
                "label": "Test Source",
                "url": "https://test.com",
                "mode": "rolling_log",
            },
        ]

        with patch("news_bot.manage_sources.storage.get_sources", return_value=mock_sources):
            cmd_list()

        output = capsys.readouterr().out
        assert "1 source(s):" in output
        assert "test_src" in output
        assert "Test Source" in output
        assert "https://test.com" in output
        assert "rolling_log" in output

    def test_prints_empty_message(self, capsys):
        from news_bot.manage_sources import cmd_list

        with patch("news_bot.manage_sources.storage.get_sources", return_value=[]):
            cmd_list()

        output = capsys.readouterr().out
        assert "No sources configured." in output


# ---------------------------------------------------------------------------
# cmd_add
# ---------------------------------------------------------------------------


class TestCmdAdd:
    """Tests for the 'add' command."""

    def test_adds_source_successfully(self, capsys):
        from news_bot.manage_sources import cmd_add

        with patch("news_bot.manage_sources.storage.add_source") as mock_add:
            cmd_add(["new_id", "New Label", "https://new.com", "article_list"])

        mock_add.assert_called_once_with("new_id", "New Label", "https://new.com", "article_list")
        output = capsys.readouterr().out
        assert "Added" in output

    def test_defaults_to_rolling_log(self, capsys):
        from news_bot.manage_sources import cmd_add

        with patch("news_bot.manage_sources.storage.add_source") as mock_add:
            cmd_add(["new_id", "New Label", "https://new.com"])

        mock_add.assert_called_once_with("new_id", "New Label", "https://new.com", "rolling_log")

    def test_exits_on_too_few_args(self):
        from news_bot.manage_sources import cmd_add

        with pytest.raises(SystemExit, match="1"):
            cmd_add(["only_id"])

    def test_exits_on_duplicate_id(self):
        from news_bot.manage_sources import cmd_add

        with (
            patch(
                "news_bot.manage_sources.storage.add_source",
                side_effect=ValueError("already exists"),
            ),
            pytest.raises(SystemExit, match="1"),
        ):
            cmd_add(["dup", "Dup", "https://dup.com"])


# ---------------------------------------------------------------------------
# cmd_remove
# ---------------------------------------------------------------------------


class TestCmdRemove:
    """Tests for the 'remove' command."""

    def test_removes_source_successfully(self, capsys):
        from news_bot.manage_sources import cmd_remove

        with patch("news_bot.manage_sources.storage.remove_source") as mock_remove:
            cmd_remove(["some_id"])

        mock_remove.assert_called_once_with("some_id")
        output = capsys.readouterr().out
        assert "Removed" in output

    def test_exits_on_no_args(self):
        from news_bot.manage_sources import cmd_remove

        with pytest.raises(SystemExit, match="1"):
            cmd_remove([])

    def test_exits_on_nonexistent_source(self):
        from news_bot.manage_sources import cmd_remove

        with (
            patch(
                "news_bot.manage_sources.storage.remove_source",
                side_effect=ValueError("No source found"),
            ),
            pytest.raises(SystemExit, match="1"),
        ):
            cmd_remove(["nonexistent"])


# ---------------------------------------------------------------------------
# main (CLI dispatch)
# ---------------------------------------------------------------------------


class TestMain:
    """Tests for CLI argument dispatch."""

    def test_no_args_exits(self):
        from news_bot.manage_sources import main

        with (
            patch("sys.argv", ["manage_sources"]),
            pytest.raises(SystemExit, match="1"),
        ):
            main()

    def test_unknown_command_exits(self):
        from news_bot.manage_sources import main

        with (
            patch("sys.argv", ["manage_sources", "unknown"]),
            pytest.raises(SystemExit, match="1"),
        ):
            main()

    def test_list_command_dispatches(self):
        from news_bot.manage_sources import main

        with (
            patch("sys.argv", ["manage_sources", "list"]),
            patch("news_bot.manage_sources.cmd_list") as mock_list,
        ):
            main()

        mock_list.assert_called_once()
