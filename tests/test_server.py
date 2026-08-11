"""Tests for MCP server tools.

Tests the 6 MCP tools exposed by server.py:
- list_accounts
- list_mailboxes
- get_emails
- get_email
- search

Uses mocking to avoid actual JXA execution (which requires macOS + Mail.app).
"""

from __future__ import annotations
from tests._mocks import mock_acct_map as _mock_acct_map
from tests._mocks import mock_index as _mock_index

import json
import os
import shutil
import threading
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _acct_map(uuid_to_name="Work", excluded_uuids=None):
    """An AccountMap double for the Envelope-Index fast path."""
    m = MagicMock()
    m.ensure_loaded = AsyncMock()
    m.names_to_uuids.return_value = set(excluded_uuids or [])
    m.name_to_uuid.return_value = None
    m.uuid_to_name.return_value = uuid_to_name
    m.get_cached_accounts.return_value = [
        {"id": "uuid-work", "name": uuid_to_name}
    ]
    return m


@pytest.fixture(autouse=True)
def _isolate_server_singletons(monkeypatch):
    """Clear AccountMap state and force Strategy 0 unavailable.

    Without the cache reset, an AccountMap populated by one test
    leaks into the next and tests that mock `execute_with_core_async`
    never actually invoke the mock.

    Without the envelope-index disable, existing tests that mock
    `execute_query_async` (the JXA fallback for get_emails) would
    be bypassed because the envelope-index SQLite would be read
    directly on a real macOS test host. Tests that want to
    exercise Strategy 0 explicitly override these patches via
    `monkeypatch.setattr(...)` of their own.
    """
    from pathlib import Path

    from apple_mail_mcp.index.accounts import AccountMap

    AccountMap.get_instance().reset()
    monkeypatch.setattr(
        "apple_mail_mcp.index.envelope_direct.envelope_index_path",
        lambda mail_dir: Path("/nonexistent/Envelope Index"),
    )
    yield
    AccountMap.get_instance().reset()


class TestListAccounts:
    """Tests for list_accounts() tool."""

    @pytest.mark.asyncio
    @patch("apple_mail_mcp.server.execute_with_core_async")
    async def test_returns_account_list(self, mock_exec):
        """list_accounts returns list of account dicts."""
        mock_exec.return_value = [
            {"name": "Work", "id": "abc123"},
            {"name": "Personal", "id": "def456"},
        ]

        from apple_mail_mcp.server import list_accounts

        result = await list_accounts()

        assert len(result) == 2
        assert result[0]["name"] == "Work"
        assert result[1]["name"] == "Personal"
        mock_exec.assert_called_once()

    @pytest.mark.asyncio
    @patch("apple_mail_mcp.server.execute_with_core_async")
    async def test_returns_empty_list_when_no_accounts(self, mock_exec):
        """list_accounts handles empty account list."""
        mock_exec.return_value = []

        from apple_mail_mcp.server import list_accounts

        result = await list_accounts()

        assert result == []


class TestListMailboxes:
    """Tests for list_mailboxes() tool."""

    @pytest.mark.asyncio
    @patch("apple_mail_mcp.server.execute_with_core_async")
    async def test_returns_mailbox_list(self, mock_exec):
        """list_mailboxes returns list of mailbox dicts."""
        mock_exec.return_value = [
            {"name": "INBOX", "unreadCount": 5},
            {"name": "Sent", "unreadCount": 0},
        ]

        from apple_mail_mcp.server import list_mailboxes

        result = await list_mailboxes("Work")

        assert len(result) == 2
        assert result[0]["name"] == "INBOX"
        assert result[0]["unreadCount"] == 5

    @pytest.mark.asyncio
    @patch("apple_mail_mcp.server.execute_with_core_async")
    async def test_uses_default_account_when_none(self, mock_exec):
        """list_mailboxes uses default account when not specified."""
        mock_exec.return_value = []

        from apple_mail_mcp.server import list_mailboxes

        await list_mailboxes(None)

        # Should still call execute - the script handles None account
        mock_exec.assert_called_once()


class TestGetEmails:
    """Tests for get_emails() tool."""

    @pytest.mark.asyncio
    @patch("apple_mail_mcp.server.execute_query_async")
    async def test_filter_all_returns_emails(self, mock_exec):
        """get_emails with filter='all' returns all emails."""
        mock_exec.return_value = [
            {
                "id": 1,
                "subject": "Test",
                "sender": "test@example.com",
                "date_received": "2024-01-15T10:00:00",
                "read": True,
                "flagged": False,
            }
        ]

        from apple_mail_mcp.server import get_emails

        result = await get_emails(filter="all")

        assert len(result) == 1
        assert result[0]["subject"] == "Test"

    @pytest.mark.asyncio
    @patch("apple_mail_mcp.server.execute_query_async")
    async def test_filter_unread_adds_read_status_condition(self, mock_exec):
        """get_emails with filter='unread' adds appropriate filter."""
        mock_exec.return_value = []

        from apple_mail_mcp.server import get_emails

        await get_emails(filter="unread")

        # Verify the query was built with the unread filter
        call_args = mock_exec.call_args[0][0]  # First positional arg (query)
        script = call_args.build()
        assert "readStatus[i] === false" in script

    @pytest.mark.asyncio
    @patch("apple_mail_mcp.server.execute_query_async")
    async def test_filter_flagged_adds_flagged_condition(self, mock_exec):
        """get_emails with filter='flagged' adds flagged filter."""
        mock_exec.return_value = []

        from apple_mail_mcp.server import get_emails

        await get_emails(filter="flagged")

        call_args = mock_exec.call_args[0][0]
        script = call_args.build()
        assert "flaggedStatus[i] === true" in script

    @pytest.mark.asyncio
    @patch("apple_mail_mcp.server.execute_query_async")
    async def test_filter_today_uses_mailcore_today(self, mock_exec):
        """get_emails with filter='today' uses MailCore.today()."""
        mock_exec.return_value = []

        from apple_mail_mcp.server import get_emails

        await get_emails(filter="today")

        call_args = mock_exec.call_args[0][0]
        script = call_args.build()
        assert "MailCore.today()" in script

    @pytest.mark.asyncio
    @patch("apple_mail_mcp.server.execute_query_async")
    async def test_filter_last_7_days_uses_days_ago(self, mock_exec):
        """get_emails with filter='last_7_days' uses MailCore.daysAgo(7)."""
        mock_exec.return_value = []

        from apple_mail_mcp.server import get_emails

        await get_emails(filter="last_7_days")

        call_args = mock_exec.call_args[0][0]
        script = call_args.build()
        assert "MailCore.daysAgo(7)" in script

    @pytest.mark.asyncio
    @patch("apple_mail_mcp.server.execute_query_async")
    async def test_filter_this_week_alias(self, mock_exec):
        """get_emails with filter='this_week' works as alias for last_7_days."""
        mock_exec.return_value = []

        from apple_mail_mcp.server import get_emails

        await get_emails(filter="this_week")

        call_args = mock_exec.call_args[0][0]
        script = call_args.build()
        assert "MailCore.daysAgo(7)" in script

    @pytest.mark.asyncio
    @patch("apple_mail_mcp.server.execute_query_async")
    async def test_respects_limit_parameter(self, mock_exec):
        """get_emails respects the limit parameter."""
        mock_exec.return_value = []

        from apple_mail_mcp.server import get_emails

        await get_emails(limit=10)

        call_args = mock_exec.call_args[0][0]
        script = call_args.build()
        # The limit appears in the loop condition
        assert "results.length < 10" in script

    @pytest.mark.asyncio
    @patch("apple_mail_mcp.server.execute_query_async")
    async def test_uses_specified_account_and_mailbox(self, mock_exec):
        """get_emails uses specified account and mailbox."""
        mock_exec.return_value = []

        from apple_mail_mcp.server import get_emails

        await get_emails(account="Work", mailbox="INBOX")

        call_args = mock_exec.call_args[0][0]
        script = call_args.build()
        assert '"Work"' in script
        assert '"INBOX"' in script


class TestGetEmail:
    """Tests for get_email() tool."""

    @pytest.mark.asyncio
    @patch("apple_mail_mcp.server._get_index_manager")
    @patch("apple_mail_mcp.server.execute_with_core_async")
    async def test_returns_full_email(self, mock_exec, mock_mgr):
        """get_email returns complete email with content."""
        mock_mgr.return_value.has_index.return_value = False
        mock_exec.return_value = {
            "id": 12345,
            "subject": "Meeting notes",
            "sender": "boss@company.com",
            "content": "Here are the notes from today's meeting...",
            "date_received": "2024-01-15T10:00:00",
            "date_sent": "2024-01-15T09:58:00",
            "read": True,
            "flagged": False,
            "reply_to": "boss@company.com",
            "message_id": "<abc123@mail.example.com>",
        }

        from apple_mail_mcp.server import get_email

        result = await get_email(12345)

        assert result["id"] == 12345
        assert result["subject"] == "Meeting notes"
        assert "notes from today" in result["content"]

    @pytest.mark.asyncio
    @patch("apple_mail_mcp.server._get_index_manager")
    @patch("apple_mail_mcp.server.execute_with_core_async")
    async def test_includes_message_id_in_script(self, mock_exec, mock_mgr):
        """get_email includes message_id in the JXA script."""
        mock_mgr.return_value.has_index.return_value = False
        mock_exec.return_value = {"id": 99999}

        from apple_mail_mcp.server import get_email

        await get_email(99999, account="Work", mailbox="INBOX")

        call_args = mock_exec.call_args[0][0]  # First positional arg
        assert "99999" in call_args
        assert "targetId" in call_args

    @pytest.mark.asyncio
    @patch("apple_mail_mcp.server._get_account_map")
    @patch("apple_mail_mcp.server._get_index_manager")
    @patch("apple_mail_mcp.server.execute_with_core_async")
    async def test_strategy0_reads_from_disk(
        self, mock_exec, mock_mgr, mock_acct_map
    ):
        """Strategy 0 reads directly from .emlx without JXA."""
        from pathlib import Path
        from unittest.mock import AsyncMock

        from apple_mail_mcp.index.disk import EmlxEmail

        parsed = EmlxEmail(
            id=42,
            subject="Disk email",
            sender="alice@example.com",
            content="Read from disk",
            date_received="2025-01-01T00:00:00",
            emlx_path=Path("/tmp/fake.emlx"),
            read=True,
            flagged=False,
            date_sent="2025-01-01T00:00:00",
            reply_to="",
            message_id_header="<abc@example.com>",
        )

        mock_mgr.return_value.has_index.return_value = True
        mock_mgr.return_value.find_email_path.return_value = Path(
            "/tmp/fake.emlx"
        )
        mock_mgr.return_value.get_email_attachments.return_value = []

        acct_map = mock_acct_map.return_value
        acct_map.ensure_loaded = AsyncMock()
        acct_map.name_to_uuid.return_value = None

        with (
            patch(
                "apple_mail_mcp.server.asyncio.to_thread",
                return_value=parsed,
            ),
            patch("pathlib.Path.exists", return_value=True),
        ):
            from apple_mail_mcp.server import get_email

            result = await get_email(42)

        assert result["id"] == 42
        assert result["subject"] == "Disk email"
        assert result["read"] is True
        assert result["message_id"] == "<abc@example.com>"
        # JXA should NOT have been called
        mock_exec.assert_not_called()

    @pytest.mark.asyncio
    @patch("apple_mail_mcp.server._get_account_map")
    @patch("apple_mail_mcp.server._get_index_manager")
    @patch("apple_mail_mcp.server.execute_with_core_async")
    async def test_strategy0_falls_through_on_failure(
        self, mock_exec, mock_mgr, mock_acct_map
    ):
        """Strategy 0 failure falls through to JXA strategies."""
        from unittest.mock import AsyncMock

        # Strategy 0: index exists but find_email_path returns None
        mock_mgr.return_value.has_index.return_value = True
        mock_mgr.return_value.find_email_path.return_value = None
        mock_mgr.return_value.get_email_attachments.return_value = []

        acct_map = mock_acct_map.return_value
        acct_map.ensure_loaded = AsyncMock()
        acct_map.name_to_uuid.return_value = None

        # Strategy 1 (JXA) should be called as fallback
        mock_exec.return_value = {
            "id": 42,
            "subject": "From JXA",
            "sender": "a@b.com",
            "content": "Body",
            "date_received": "2024-01-01",
            "date_sent": "2024-01-01",
            "read": True,
            "flagged": False,
            "reply_to": "",
            "message_id": "<x>",
            "attachments": [],
        }

        from apple_mail_mcp.server import get_email

        result = await get_email(42, account="Work", mailbox="INBOX")

        assert result["subject"] == "From JXA"
        mock_exec.assert_called()

    @pytest.mark.asyncio
    @patch("apple_mail_mcp.server._get_account_map")
    @patch("apple_mail_mcp.server._get_index_manager")
    @patch("apple_mail_mcp.server.execute_with_core_async")
    async def test_strategy0_cleans_stale_index_entry(
        self, mock_exec, mock_mgr, mock_acct_map
    ):
        """Stale FTS5 entry is auto-cleaned — and the search CONTINUES.

        A missing .emlx means the recorded path is wrong, nothing more:
        the message may have been re-filed, or Mail may have rebuilt its
        store. The original behaviour raised "deleted or moved" here on
        the assumption that the live strategies would fail anyway; that
        assumption was never checked, and it made the tool state an
        absence it had not established. (#74, corrected after review.)
        """
        from pathlib import Path
        from unittest.mock import AsyncMock

        # Strategy 0: index has a path, but the file is gone on disk
        mock_mgr.return_value.has_index.return_value = True
        mock_mgr.return_value.find_email_path.return_value = Path(
            "/nonexistent/42.emlx"
        )
        mock_mgr.return_value.delete_email.return_value = 1

        acct_map = mock_acct_map.return_value
        acct_map.ensure_loaded = AsyncMock()
        acct_map.name_to_uuid.return_value = "uuid-1"

        from apple_mail_mcp.server import get_email

        mock_exec.side_effect = RuntimeError("Message not found (-1728)")

        with patch("pathlib.Path.exists", return_value=False):
            with pytest.raises(ValueError, match="not found"):
                await get_email(42, account="Work", mailbox="INBOX")

        # The stale entry was cleaned up with the resolved account UUID
        mock_mgr.return_value.delete_email.assert_called_once_with(
            42, account="uuid-1", mailbox="INBOX"
        )
        # Apple Mail was actually asked before absence was reported.
        mock_exec.assert_called()

    @pytest.mark.asyncio
    async def test_get_email_uses_index_for_fallback(self):
        """B1: Strategy 2 uses index lookup when strategy 1 fails."""
        call_count = 0

        async def mock_exec_side_effect(script, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise Exception("Not found in specified mailbox")
            if call_count == 2:
                # Strategy 2 succeeds
                return {
                    "id": 42,
                    "subject": "Found via index",
                    "sender": "a@b.com",
                    "content": "Body",
                    "date_received": "2024-01-01",
                    "date_sent": "2024-01-01",
                    "read": True,
                    "flagged": False,
                    "reply_to": "",
                    "message_id": "<x>",
                    "attachments": [],
                }
            return {}

        mock_manager = MagicMock()
        mock_manager.has_index.return_value = True
        mock_manager.find_email_location.return_value = (
            "uuid-123",
            "Archive",
        )
        mock_manager.get_email_attachments.return_value = None

        mock_acct_map = MagicMock()
        mock_acct_map.ensure_loaded = AsyncMock()
        mock_acct_map.uuid_to_name.return_value = "Work"

        with (
            patch(
                "apple_mail_mcp.server.execute_with_core_async",
                side_effect=mock_exec_side_effect,
            ),
            patch("apple_mail_mcp.server._get_index_manager") as mock_get_mgr,
            patch("apple_mail_mcp.server._get_account_map") as mock_get_map,
        ):
            mock_get_mgr.return_value = mock_manager
            mock_get_map.return_value = mock_acct_map

            from apple_mail_mcp.server import get_email

            result = await get_email(42)

            assert result["subject"] == "Found via index"
            assert call_count == 2  # Strategy 1 failed, 2 succeeded


class TestSearch:
    """Tests for search() tool."""

    @pytest.mark.asyncio
    async def test_uses_fts_when_index_available(self, populated_db):
        """search uses FTS5 path when index exists."""
        mock_manager = MagicMock()
        mock_manager.has_index.return_value = True

        mock_result = MagicMock()
        mock_result.id = 1001
        mock_result.subject = "Invoice #12345"
        mock_result.sender = "billing@vendor.com"
        mock_result.date_received = "2024-01-14T09:00:00"
        mock_result.score = 2.5
        mock_result.content_snippet = "Your invoice..."
        mock_result.account = "test-account"
        mock_result.mailbox = "INBOX"
        mock_manager.search.return_value = [mock_result]

        mock_acct_map = MagicMock()
        mock_acct_map.ensure_loaded = AsyncMock()
        mock_acct_map.name_to_uuid.return_value = None
        mock_acct_map.uuid_to_name.side_effect = lambda x: x

        with (
            patch("apple_mail_mcp.server._get_index_manager") as mock_get,
            patch("apple_mail_mcp.server._get_account_map") as mock_get_map,
        ):
            mock_get.return_value = mock_manager
            mock_get_map.return_value = mock_acct_map

            from apple_mail_mcp.server import search

            result = await search("invoice")

            assert len(result) == 1
            assert result[0]["subject"] == "Invoice #12345"
            # S1: matched_in is now detected dynamically
            assert "body" in result[0]["matched_in"]
            mock_manager.search.assert_called_once()

    @pytest.mark.asyncio
    async def test_fts_translates_account_name_to_uuid(self):
        """search(account="Work") translates to UUID for FTS5."""
        mock_manager = MagicMock()
        mock_manager.has_index.return_value = True
        mock_manager.search.return_value = []

        mock_acct_map = MagicMock()
        mock_acct_map.ensure_loaded = AsyncMock()
        mock_acct_map.name_to_uuid.return_value = "UUID-WORK-123"

        with (
            patch("apple_mail_mcp.server._get_index_manager") as mock_get,
            patch("apple_mail_mcp.server._get_account_map") as mock_get_map,
        ):
            mock_get.return_value = mock_manager
            mock_get_map.return_value = mock_acct_map

            from apple_mail_mcp.server import search

            await search("invoice", account="Work")

            # Verify manager.search received the UUID, not "Work"
            call_kwargs = mock_manager.search.call_args[1]
            assert call_kwargs["account"] == "UUID-WORK-123"

    @pytest.mark.asyncio
    async def test_fts_results_show_friendly_account_name(self):
        """FTS5 results translate UUID back to friendly name."""
        mock_manager = MagicMock()
        mock_manager.has_index.return_value = True

        mock_result = MagicMock()
        mock_result.id = 1
        mock_result.subject = "Test"
        mock_result.sender = "a@b.com"
        mock_result.date_received = "2024-01-01"
        mock_result.score = 1.0
        mock_result.content_snippet = "..."
        mock_result.account = "UUID-WORK-123"
        mock_result.mailbox = "INBOX"
        mock_manager.search.return_value = [mock_result]

        mock_acct_map = MagicMock()
        mock_acct_map.ensure_loaded = AsyncMock()
        mock_acct_map.name_to_uuid.return_value = None
        mock_acct_map.uuid_to_name.return_value = "Work"

        with (
            patch("apple_mail_mcp.server._get_index_manager") as mock_get,
            patch("apple_mail_mcp.server._get_account_map") as mock_get_map,
        ):
            mock_get.return_value = mock_manager
            mock_get_map.return_value = mock_acct_map

            from apple_mail_mcp.server import search

            result = await search("test")

            # Result should show "Work", not "UUID-WORK-123"
            assert result[0]["account"] == "Work"

    @pytest.mark.asyncio
    async def test_fts_account_filter_falls_back_to_raw_value(
        self,
    ):
        """If name isn't in AccountMap, pass it through as-is."""
        mock_manager = MagicMock()
        mock_manager.has_index.return_value = True
        mock_manager.search.return_value = []

        mock_acct_map = MagicMock()
        mock_acct_map.ensure_loaded = AsyncMock()
        mock_acct_map.name_to_uuid.return_value = None  # Not found

        with (
            patch("apple_mail_mcp.server._get_index_manager") as mock_get,
            patch("apple_mail_mcp.server._get_account_map") as mock_get_map,
        ):
            mock_get.return_value = mock_manager
            mock_get_map.return_value = mock_acct_map

            from apple_mail_mcp.server import search

            await search("test", account="RAW-UUID-ABC")

            # Should pass through the raw value as fallback
            call_kwargs = mock_manager.search.call_args[1]
            assert call_kwargs["account"] == "RAW-UUID-ABC"

    @pytest.mark.asyncio
    @patch("apple_mail_mcp.server.execute_query_async")
    async def test_falls_back_to_jxa_when_no_index(self, mock_exec):
        """search falls back to JXA when no FTS5 index exists."""
        mock_exec.return_value = [
            {
                "id": 1,
                "subject": "Test Invoice",
                "sender": "test@example.com",
                "date_received": "2024-01-15T10:00:00",
                "read": True,
                "flagged": False,
            }
        ]

        mock_manager = MagicMock()
        mock_manager.has_index.return_value = False

        with patch("apple_mail_mcp.server._get_index_manager") as mock_get:
            mock_get.return_value = mock_manager

            from apple_mail_mcp.server import search

            result = await search("invoice")

            # Should use JXA path
            mock_exec.assert_called_once()
            assert len(result) == 1

    @pytest.mark.asyncio
    async def test_scope_subject_uses_fts_column(self):
        """search with scope='subject' uses FTS5 column filter."""
        mock_manager = MagicMock()
        mock_manager.has_index.return_value = True
        mock_manager.search.return_value = []

        mock_acct_map = MagicMock()
        mock_acct_map.ensure_loaded = AsyncMock()
        mock_acct_map.name_to_uuid.return_value = None

        with (
            patch("apple_mail_mcp.server._get_index_manager") as mock_get,
            patch("apple_mail_mcp.server._get_account_map") as mock_get_map,
        ):
            mock_get.return_value = mock_manager
            mock_get_map.return_value = mock_acct_map

            from apple_mail_mcp.server import search

            await search("urgent", scope="subject")

            mock_manager.search.assert_called_once()
            call_kwargs = mock_manager.search.call_args
            assert call_kwargs.kwargs.get("column") == "subject"

    @pytest.mark.asyncio
    async def test_scope_sender_uses_fts_column(self):
        """search with scope='sender' uses FTS5 column filter."""
        mock_manager = MagicMock()
        mock_manager.has_index.return_value = True
        mock_manager.search.return_value = []

        mock_acct_map = MagicMock()
        mock_acct_map.ensure_loaded = AsyncMock()
        mock_acct_map.name_to_uuid.return_value = None

        with (
            patch("apple_mail_mcp.server._get_index_manager") as mock_get,
            patch("apple_mail_mcp.server._get_account_map") as mock_get_map,
        ):
            mock_get.return_value = mock_manager
            mock_get_map.return_value = mock_acct_map

            from apple_mail_mcp.server import search

            await search("john@example.com", scope="sender")

            mock_manager.search.assert_called_once()
            call_kwargs = mock_manager.search.call_args
            assert call_kwargs.kwargs.get("column") == "sender"

    @pytest.mark.asyncio
    async def test_scope_body_uses_fts(self):
        """search with scope='body' uses FTS5 path when available."""
        mock_manager = MagicMock()
        mock_manager.has_index.return_value = True
        mock_manager.search.return_value = []

        mock_acct_map = MagicMock()
        mock_acct_map.ensure_loaded = AsyncMock()
        mock_acct_map.name_to_uuid.return_value = None

        with (
            patch("apple_mail_mcp.server._get_index_manager") as mock_get,
            patch("apple_mail_mcp.server._get_account_map") as mock_get_map,
        ):
            mock_get.return_value = mock_manager
            mock_get_map.return_value = mock_acct_map

            from apple_mail_mcp.server import search

            await search("meeting notes", scope="body")

            mock_manager.search.assert_called_once()

    @pytest.mark.asyncio
    @patch("apple_mail_mcp.server.execute_query_async")
    async def test_respects_limit(self, mock_exec):
        """search respects limit parameter."""
        mock_exec.return_value = []

        mock_manager = MagicMock()
        mock_manager.has_index.return_value = False

        with patch("apple_mail_mcp.server._get_index_manager") as mock_get:
            mock_get.return_value = mock_manager

            from apple_mail_mcp.server import search

            await search("test", limit=5)

            call_args = mock_exec.call_args[0][0]
            script = call_args.build()
            assert "results.length < 5" in script


class TestHelperFunctions:
    """Tests for helper functions in server.py."""

    def test_resolve_account_returns_provided_account(self):
        """_resolve_account returns provided account when given."""
        from apple_mail_mcp.server import _resolve_account

        result = _resolve_account("Work")
        assert result == "Work"

    def test_resolve_account_returns_none_when_no_default(self):
        """_resolve_account returns None when no default is set."""
        from apple_mail_mcp.server import _resolve_account

        with patch("apple_mail_mcp.server.get_default_account") as mock:
            mock.return_value = None
            result = _resolve_account(None)
            assert result is None

    def test_resolve_mailbox_returns_provided_mailbox(self):
        """_resolve_mailbox returns provided mailbox when given."""
        from apple_mail_mcp.server import _resolve_mailbox

        result = _resolve_mailbox("INBOX")
        assert result == "INBOX"

    def test_resolve_mailbox_returns_default_when_none(self):
        """_resolve_mailbox returns default when None provided."""
        from apple_mail_mcp.server import _resolve_mailbox

        with patch("apple_mail_mcp.server.get_default_mailbox") as mock:
            mock.return_value = "Inbox"
            result = _resolve_mailbox(None)
            assert result == "Inbox"


class TestDetectMatchedColumns:
    """Tests for S1: accurate matched_in detection."""

    def test_detects_subject_match(self):
        from apple_mail_mcp.server import _detect_matched_columns

        result = MagicMock()
        result.subject = "Meeting tomorrow"
        result.sender = "boss@company.com"
        result.content_snippet = "Please review..."

        matched = _detect_matched_columns("meeting", result)
        assert "subject" in matched
        assert "body" in matched

    def test_detects_sender_match(self):
        from apple_mail_mcp.server import _detect_matched_columns

        result = MagicMock()
        result.subject = "Hello"
        result.sender = "john@example.com"
        result.content_snippet = "Hi there"

        matched = _detect_matched_columns("john", result)
        assert "sender" in matched

    def test_body_always_included(self):
        from apple_mail_mcp.server import _detect_matched_columns

        result = MagicMock()
        result.subject = "Other topic"
        result.sender = "other@test.com"
        result.content_snippet = "Some content"

        matched = _detect_matched_columns("xyzunknown", result)
        assert "body" in matched


class TestSearchFtsAccountFiltering:
    """Tests for S5: FTS5 None account means all."""

    @pytest.mark.asyncio
    async def test_search_fts_none_account_means_all(self):
        """When account=None, FTS5 path should NOT resolve a default."""
        mock_manager = MagicMock()
        mock_manager.has_index.return_value = True
        mock_manager.is_stale.return_value = False
        mock_manager.search.return_value = []

        mock_acct_map = MagicMock()
        mock_acct_map.ensure_loaded = AsyncMock()
        mock_acct_map.name_to_uuid.return_value = None

        with (
            patch("apple_mail_mcp.server._get_index_manager") as mock_get,
            patch("apple_mail_mcp.server._get_account_map") as mock_get_map,
        ):
            mock_get.return_value = mock_manager
            mock_get_map.return_value = mock_acct_map

            from apple_mail_mcp.server import search

            await search("test", account=None)

            # account should be None → search all
            call_kwargs = mock_manager.search.call_args[1]
            assert call_kwargs["account"] is None


class TestSearchNoAutoSync:
    """Tests for #51: search no longer auto-syncs."""

    @pytest.mark.asyncio
    async def test_search_does_not_auto_sync(self):
        """Search does NOT trigger sync (handled by background thread)."""
        mock_manager = MagicMock()
        mock_manager.has_index.return_value = True
        mock_manager.search.return_value = []

        mock_acct_map = MagicMock()
        mock_acct_map.ensure_loaded = AsyncMock()
        mock_acct_map.name_to_uuid.return_value = None

        with (
            patch("apple_mail_mcp.server._get_index_manager") as mock_get,
            patch("apple_mail_mcp.server._get_account_map") as mock_get_map,
        ):
            mock_get.return_value = mock_manager
            mock_get_map.return_value = mock_acct_map

            from apple_mail_mcp.server import search

            await search("test")

            # sync_updates should NOT be called
            mock_manager.sync_updates.assert_not_called()


class TestSearchExcludeMailboxes:
    """Tests for S3: draft exclusion in search."""

    @pytest.mark.asyncio
    async def test_search_excludes_drafts_by_default(self):
        """Search passes exclude_mailboxes=["Drafts"] by default."""
        mock_manager = MagicMock()
        mock_manager.has_index.return_value = True
        mock_manager.is_stale.return_value = False
        mock_manager.search.return_value = []

        mock_acct_map = MagicMock()
        mock_acct_map.ensure_loaded = AsyncMock()
        mock_acct_map.name_to_uuid.return_value = None

        with (
            patch("apple_mail_mcp.server._get_index_manager") as mock_get,
            patch("apple_mail_mcp.server._get_account_map") as mock_get_map,
        ):
            mock_get.return_value = mock_manager
            mock_get_map.return_value = mock_acct_map

            from apple_mail_mcp.server import search

            await search("test")

            call_kwargs = mock_manager.search.call_args[1]
            assert call_kwargs["exclude_mailboxes"] == ["Drafts"]


class TestGetAttachment:
    """Tests for A4: get_attachment tool."""

    @pytest.mark.asyncio
    async def test_get_attachment_returns_file_path(self, tmp_path):
        """get_attachment saves to file and returns path."""
        from pathlib import Path

        mock_manager = MagicMock()
        mock_manager.has_index.return_value = True
        mock_manager.find_email_path.return_value = Path("/fake/path/42.emlx")

        fake_bytes = b"fake pdf content"
        fake_result = (fake_bytes, "application/pdf")

        with (
            patch("apple_mail_mcp.server._get_index_manager") as mock_get,
            patch(
                "apple_mail_mcp.server.asyncio.to_thread",
                new_callable=AsyncMock,
            ) as mock_thread,
            patch(
                "apple_mail_mcp.server.ATTACHMENT_CACHE_DIR",
                tmp_path / "attachments",
            ),
        ):
            mock_get.return_value = mock_manager
            mock_thread.return_value = fake_result

            from apple_mail_mcp.server import get_attachment

            result = await get_attachment(42, "invoice.pdf")

            assert result["filename"] == "invoice.pdf"
            assert result["mime_type"] == "application/pdf"
            assert result["size"] == len(fake_bytes)
            assert "file_path" in result
            assert "content_base64" not in result

    @pytest.mark.asyncio
    async def test_get_attachment_raises_for_missing(self):
        """get_attachment raises ValueError for missing attachment."""
        from pathlib import Path

        mock_manager = MagicMock()
        mock_manager.has_index.return_value = True
        mock_manager.find_email_path.return_value = Path("/fake/path/42.emlx")

        with (
            patch("apple_mail_mcp.server._get_index_manager") as mock_get,
            patch(
                "apple_mail_mcp.server.asyncio.to_thread",
                new_callable=AsyncMock,
            ) as mock_thread,
        ):
            mock_get.return_value = mock_manager
            mock_thread.return_value = None

            from apple_mail_mcp.server import get_attachment

            with pytest.raises(ValueError, match="not found"):
                await get_attachment(42, "missing.pdf")

    @pytest.mark.asyncio
    async def test_cached_attachment_file_is_owner_only(self, tmp_path):
        """Cached attachment file is chmod'd to 0o600.

        Defense-in-depth: the cache directory is already 0o700, but the
        file itself should also be owner-only so it stays protected if a
        later refactor changes the parent dir's mode or if the file is
        copied/moved out of the cache.
        """
        import stat as stat_mod
        from pathlib import Path

        mock_manager = MagicMock()
        mock_manager.has_index.return_value = True
        mock_manager.find_email_path.return_value = Path("/fake/42.emlx")

        with (
            patch("apple_mail_mcp.server._get_index_manager") as mock_get,
            patch(
                "apple_mail_mcp.server.asyncio.to_thread",
                new_callable=AsyncMock,
            ) as mock_thread,
            patch(
                "apple_mail_mcp.server.ATTACHMENT_CACHE_DIR",
                tmp_path / "attachments",
            ),
        ):
            mock_get.return_value = mock_manager
            mock_thread.return_value = (b"secret bytes", "application/pdf")

            from apple_mail_mcp.server import get_attachment

            result = await get_attachment(42, "private.pdf")
            file_path = Path(result["file_path"])
            mode = stat_mod.S_IMODE(file_path.stat().st_mode)
            assert mode == 0o600, f"Expected 0o600 permissions, got {oct(mode)}"


class TestSearchAttachments:
    """Tests for A5: search by attachment filename."""

    @pytest.mark.asyncio
    async def test_search_scope_attachments(self):
        """search(scope='attachments') queries attachments table."""
        mock_manager = MagicMock()
        mock_manager.has_index.return_value = True
        mock_manager.search_attachments.return_value = [
            {
                "message_id": 1,
                "account": "UUID-123",
                "mailbox": "INBOX",
                "subject": "Invoice attached",
                "sender": "billing@co.com",
                "date_received": "2024-01-15",
                "rfc822_message_id": "<inv-1@co.com>",
                "filename": "invoice.pdf",
            }
        ]

        mock_acct_map = MagicMock()
        mock_acct_map.ensure_loaded = AsyncMock()
        mock_acct_map.uuid_to_name.return_value = "Work"

        with (
            patch("apple_mail_mcp.server._get_index_manager") as mock_get,
            patch("apple_mail_mcp.server._get_account_map") as mock_get_map,
        ):
            mock_get.return_value = mock_manager
            mock_get_map.return_value = mock_acct_map

            from apple_mail_mcp.server import search

            results = await search("invoice", scope="attachments")

            assert len(results) == 1
            assert results[0]["matched_in"] == "attachment: invoice.pdf"
            assert results[0]["account"] == "Work"


class TestGetEmailEnrichesAttachments:
    """Tests for #36: attachment enrichment from index."""

    @pytest.mark.asyncio
    async def test_enriches_attachments_from_index(self):
        """get_email replaces JXA attachments with richer index data."""
        jxa_result = {
            "id": 42,
            "subject": "Test",
            "sender": "a@b.com",
            "content": "Body",
            "date_received": "2024-01-01",
            "date_sent": "2024-01-01",
            "read": True,
            "flagged": False,
            "reply_to": "",
            "message_id": "<x>",
            "attachments": [
                {
                    "filename": "doc.pdf",
                    "mime_type": "application/pdf",
                    "size": 100,
                }
            ],
        }
        idx_atts = [
            {
                "filename": "doc.pdf",
                "mime_type": "application/pdf",
                "size": 100,
                "content_id": None,
            },
            {
                "filename": "sig.p7s",
                "mime_type": "application/pkcs7-signature",
                "size": 50,
                "content_id": None,
            },
        ]

        mock_manager = MagicMock()
        mock_manager.has_index.return_value = True
        mock_manager.get_email_attachments.return_value = idx_atts

        with (
            patch(
                "apple_mail_mcp.server.execute_with_core_async",
                new_callable=AsyncMock,
                return_value=jxa_result,
            ),
            patch("apple_mail_mcp.server._get_index_manager") as mock_get_mgr,
        ):
            mock_get_mgr.return_value = mock_manager

            from apple_mail_mcp.server import get_email

            result = await get_email(42)

            # Index has 2 attachments vs JXA's 1, so index wins
            assert len(result["attachments"]) == 2
            assert result["attachments"][1]["filename"] == "sig.p7s"


class TestStrategy3Timeout:
    """Tests for #40: Strategy 3 timeout guard."""

    @pytest.mark.asyncio
    async def test_get_email_strategy3_has_timeout(self):
        """Strategy 3 passes timeout=15 to execute_with_core_async."""
        call_count = 0

        async def mock_exec(script, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # Strategy 1 fails
                raise Exception("Not found in mailbox")
            # Strategy 3 (Strategy 2 skipped: has_index=False)
            assert kwargs.get("timeout") == 15
            return {
                "id": 42,
                "subject": "Found",
                "sender": "a@b.com",
                "content": "Body",
                "date_received": "2024-01-01",
                "date_sent": "2024-01-01",
                "read": True,
                "flagged": False,
                "reply_to": "",
                "message_id": "<x>",
                "attachments": [],
            }

        mock_manager = MagicMock()
        mock_manager.has_index.return_value = False
        mock_manager.get_email_attachments.return_value = None

        with (
            patch(
                "apple_mail_mcp.server.execute_with_core_async",
                side_effect=mock_exec,
            ),
            patch("apple_mail_mcp.server._get_index_manager") as mock_get_mgr,
        ):
            mock_get_mgr.return_value = mock_manager

            from apple_mail_mcp.server import get_email

            result = await get_email(42)
            assert result["subject"] == "Found"
            assert call_count == 2  # Strategy 1 + Strategy 3


class TestIndexStatusResource:
    """Tests for the index://status MCP resource (#12)."""

    @pytest.mark.asyncio
    @patch("apple_mail_mcp.server._get_index_manager")
    async def test_no_index(self, mock_mgr):
        """Returns has_index=False when the index hasn't been built."""

        mock_mgr.return_value.has_index.return_value = False

        from apple_mail_mcp.server import index_status

        result = await index_status()
        data = json.loads(result)

        assert data["has_index"] is False
        assert "apple-mail-mcp index" in data["message"]

    @pytest.mark.asyncio
    @patch("apple_mail_mcp.server._get_index_manager")
    async def test_with_index(self, mock_mgr):
        """Returns the stats payload with all expected fields."""
        from datetime import datetime

        from apple_mail_mcp.index.manager import IndexStats

        mock_mgr.return_value.has_index.return_value = True
        mock_mgr.return_value.get_stats.return_value = IndexStats(
            email_count=12345,
            mailbox_count=8,
            last_sync=datetime(2026, 5, 1, 12, 0, 0),
            db_size_mb=42.5678,
            staleness_hours=2.4567,
            capped_mailboxes=1,
            attachment_count=42,
            disk_email_count=12400,
        )

        from apple_mail_mcp.server import index_status

        result = await index_status()
        data = json.loads(result)

        assert data["has_index"] is True
        assert data["email_count"] == 12345
        assert data["mailbox_count"] == 8
        assert data["attachment_count"] == 42
        assert data["disk_email_count"] == 12400
        assert data["db_size_mb"] == 42.57  # rounded
        assert data["capped_mailboxes"] == 1
        assert data["last_sync"] == "2026-05-01T12:00:00"
        assert data["staleness_hours"] == 2.46  # rounded

    @pytest.mark.asyncio
    @patch("apple_mail_mcp.server._get_index_manager")
    async def test_handles_none_optional_fields(self, mock_mgr):
        """last_sync and staleness_hours can be None on a fresh DB."""

        from apple_mail_mcp.index.manager import IndexStats

        mock_mgr.return_value.has_index.return_value = True
        mock_mgr.return_value.get_stats.return_value = IndexStats(
            email_count=0,
            mailbox_count=0,
            last_sync=None,
            db_size_mb=0.0,
            staleness_hours=None,
            disk_email_count=None,
        )

        from apple_mail_mcp.server import index_status

        result = await index_status()
        data = json.loads(result)

        assert data["last_sync"] is None
        assert data["staleness_hours"] is None
        assert data["disk_email_count"] is None


class TestEnsureWritable:
    """Direct tests for the read-only guard helper (#80)."""

    def setup_method(self):
        from apple_mail_mcp.config import set_read_only_mode

        set_read_only_mode(False)

    def teardown_method(self):
        from apple_mail_mcp.config import set_read_only_mode

        set_read_only_mode(False)

    def test_no_raise_when_writable(self):
        from apple_mail_mcp.server import _ensure_writable

        _ensure_writable()  # should not raise

    def test_raises_when_programmatic_read_only(self):
        from apple_mail_mcp.config import set_read_only_mode
        from apple_mail_mcp.server import _ensure_writable

        set_read_only_mode(True)
        with pytest.raises(PermissionError, match="read-only"):
            _ensure_writable()

    def test_raises_when_env_read_only(self, monkeypatch):
        from apple_mail_mcp.server import _ensure_writable

        monkeypatch.setenv("APPLE_MAIL_READ_ONLY", "true")
        with pytest.raises(PermissionError, match="read-only"):
            _ensure_writable()


class TestWriteImplyingToolsHaveGuard:
    """Regression: every write-implying @mcp.tool must call _ensure_writable.

    Fires when a future write tool (e.g. `mark_as_read`, `move_email`,
    `send_email`) is added to server.py without the guard. Scope is the
    issue #80 foot-gun: forgetting the call, not implementing it
    incorrectly.
    """

    WRITE_PREFIXES = (
        "mark_",
        "move_",
        "send_",
        "reply_",
        "forward_",
        "delete_",
        "create_",
        "update_",
        "set_",
        "archive_",
        "trash_",
        "flag_",
        "unflag_",
    )

    def test_all_write_implying_tools_call_ensure_writable(self):
        import ast
        from pathlib import Path

        import apple_mail_mcp.server as server_module

        server_path = Path(server_module.__file__)
        tree = ast.parse(server_path.read_text())

        violations = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not self._has_mcp_tool_decorator(node):
                continue
            if not node.name.startswith(self.WRITE_PREFIXES):
                continue
            if not self._calls_ensure_writable(node):
                violations.append(node.name)

        assert not violations, (
            f"@mcp.tool functions with write-implying names must call "
            f"_ensure_writable() at entry. Missing guard in: {violations}."
        )

    @staticmethod
    def _has_mcp_tool_decorator(node) -> bool:
        import ast

        for dec in node.decorator_list:
            # @mcp.tool
            if isinstance(dec, ast.Attribute) and dec.attr == "tool":
                if isinstance(dec.value, ast.Name) and dec.value.id == "mcp":
                    return True
            # @mcp.tool(...)
            if isinstance(dec, ast.Call):
                func = dec.func
                if isinstance(func, ast.Attribute) and func.attr == "tool":
                    if (
                        isinstance(func.value, ast.Name)
                        and func.value.id == "mcp"
                    ):
                        return True
        return False

    @staticmethod
    def _calls_ensure_writable(node) -> bool:
        import ast

        for child in ast.walk(node):
            if isinstance(child, ast.Call):
                func = child.func
                if isinstance(func, ast.Name) and func.id == "_ensure_writable":
                    return True
        return False


class TestInputValidation:
    """MCP boundary validation: pagination clamps + date checks (#96)."""

    def test_pagination_clamps_negative_limit(self):
        from apple_mail_mcp.server import _validate_pagination

        assert _validate_pagination(-1) == (1, 0)

    def test_pagination_clamps_oversized_limit(self):
        from apple_mail_mcp.server import MAX_RESULT_LIMIT, _validate_pagination

        limit, _ = _validate_pagination(10_000)
        assert limit == MAX_RESULT_LIMIT

    def test_pagination_clamps_negative_offset(self):
        from apple_mail_mcp.server import _validate_pagination

        assert _validate_pagination(20, -5) == (20, 0)

    def test_pagination_passes_sane_values(self):
        from apple_mail_mcp.server import _validate_pagination

        assert _validate_pagination(20, 40) == (20, 40)

    def test_date_accepts_valid(self):
        from apple_mail_mcp.server import _validate_date

        assert _validate_date("2026-01-31", "before") == "2026-01-31"

    def test_date_accepts_none(self):
        from apple_mail_mcp.server import _validate_date

        assert _validate_date(None, "after") is None

    @pytest.mark.parametrize(
        "bad", ["2026-6-1", "2026-13-01", "yesterday", "01/31/2026", ""]
    )
    def test_date_rejects_malformed(self, bad):
        from apple_mail_mcp.server import _validate_date

        with pytest.raises(ValueError, match="YYYY-MM-DD"):
            _validate_date(bad, "before")

    def test_date_error_names_the_param(self):
        from apple_mail_mcp.server import _validate_date

        with pytest.raises(ValueError, match="after"):
            _validate_date("nope", "after")

    @pytest.mark.asyncio
    async def test_search_rejects_malformed_date(self):
        from apple_mail_mcp.server import search

        with pytest.raises(ValueError, match="YYYY-MM-DD"):
            await search("budget", before="2026-99-99")

    def test_clamped_env_int_bounds(self, monkeypatch):
        from apple_mail_mcp.server import _clamped_env_int

        monkeypatch.setenv("X_TEST_CLAMP", "999999")
        assert _clamped_env_int("X_TEST_CLAMP", 15, 1, 300) == 300
        monkeypatch.setenv("X_TEST_CLAMP", "-3")
        assert _clamped_env_int("X_TEST_CLAMP", 15, 1, 300) == 1
        monkeypatch.delenv("X_TEST_CLAMP")
        assert _clamped_env_int("X_TEST_CLAMP", 15, 1, 300) == 15


class TestAttachmentSaveToFile:
    """get_attachment should save to disk and return file_path."""

    @pytest.mark.asyncio
    async def test_get_attachment_saves_to_file(self, tmp_path: Path):
        """Attachment bytes are written to disk, path returned."""
        mock_manager = MagicMock()
        mock_manager.has_index.return_value = True
        mock_manager.find_email_path.return_value = Path("/fake/path/42.emlx")

        fake_bytes = b"fake pdf content here"
        fake_result = (fake_bytes, "application/pdf")

        with (
            patch("apple_mail_mcp.server._get_index_manager") as mock_get,
            patch(
                "apple_mail_mcp.server.asyncio.to_thread",
                new_callable=AsyncMock,
            ) as mock_thread,
            patch(
                "apple_mail_mcp.server.ATTACHMENT_CACHE_DIR",
                tmp_path / "attachments",
            ),
        ):
            mock_get.return_value = mock_manager
            mock_thread.return_value = fake_result

            from apple_mail_mcp.server import get_attachment

            result = await get_attachment(42, "invoice.pdf")

            assert result["filename"] == "invoice.pdf"
            assert result["mime_type"] == "application/pdf"
            assert result["size"] == len(fake_bytes)
            assert "file_path" in result
            assert "content_base64" not in result

            # Verify file was actually written
            saved = Path(result["file_path"])
            assert saved.exists()
            assert saved.read_bytes() == fake_bytes

    @pytest.mark.asyncio
    async def test_get_attachment_safe_filename(self, tmp_path: Path):
        """Path traversal in filename is stripped."""
        mock_manager = MagicMock()
        mock_manager.has_index.return_value = True
        mock_manager.find_email_path.return_value = Path("/fake/path/42.emlx")

        fake_result = (b"data", "text/plain")

        with (
            patch("apple_mail_mcp.server._get_index_manager") as mock_get,
            patch(
                "apple_mail_mcp.server.asyncio.to_thread",
                new_callable=AsyncMock,
            ) as mock_thread,
            patch(
                "apple_mail_mcp.server.ATTACHMENT_CACHE_DIR",
                tmp_path / "attachments",
            ),
        ):
            mock_get.return_value = mock_manager
            mock_thread.return_value = fake_result

            from apple_mail_mcp.server import get_attachment

            result = await get_attachment(42, "../../evil.txt")

            # Should strip to just "evil.txt"
            assert result["filename"] == "evil.txt"
            assert Path(result["file_path"]).name == "evil.txt"


class TestCleanupOldAttachments:
    """_cleanup_old_attachments removes stale dirs."""

    def test_cleanup_removes_old_dirs(self, tmp_path: Path):
        cache_dir = tmp_path / "attachments"
        cache_dir.mkdir()

        # Create an old subdirectory
        old_dir = cache_dir / "old_extraction"
        old_dir.mkdir()
        (old_dir / "file.pdf").write_bytes(b"old data")
        # Set mtime to 48 hours ago
        old_time = time.time() - (48 * 3600)
        os.utime(old_dir, (old_time, old_time))

        with patch("apple_mail_mcp.server.ATTACHMENT_CACHE_DIR", cache_dir):
            from apple_mail_mcp.server import _cleanup_old_attachments

            _cleanup_old_attachments(max_age_hours=24)

        assert not old_dir.exists()

    def test_cleanup_preserves_recent_dirs(self, tmp_path: Path):
        cache_dir = tmp_path / "attachments"
        cache_dir.mkdir()

        # Create a recent subdirectory
        recent_dir = cache_dir / "recent_extraction"
        recent_dir.mkdir()
        (recent_dir / "file.pdf").write_bytes(b"recent data")

        with patch("apple_mail_mcp.server.ATTACHMENT_CACHE_DIR", cache_dir):
            from apple_mail_mcp.server import _cleanup_old_attachments

            _cleanup_old_attachments(max_age_hours=24)

        assert recent_dir.exists()

    def test_cleanup_handles_missing_dir(self):
        """No error when cache dir doesn't exist."""
        with patch(
            "apple_mail_mcp.server.ATTACHMENT_CACHE_DIR",
            Path("/nonexistent/path"),
        ):
            from apple_mail_mcp.server import _cleanup_old_attachments

            _cleanup_old_attachments()  # Should not raise


class TestSearchEmptyResultHint:
    """search() returns a hint dict when no results found."""

    @pytest.mark.asyncio
    async def test_fts_empty_returns_hint(self):
        """FTS5 path returns hint when no results."""
        mock_manager = MagicMock()
        mock_manager.has_index.return_value = True
        mock_manager.search.return_value = []

        mock_acct_map = MagicMock()
        mock_acct_map.ensure_loaded = AsyncMock()

        with (
            patch("apple_mail_mcp.server._get_index_manager") as mock_get,
            patch("apple_mail_mcp.server._get_account_map") as mock_get_acct,
        ):
            mock_get.return_value = mock_manager
            mock_get_acct.return_value = mock_acct_map

            from apple_mail_mcp.server import search

            result = await search("xyznonexistent123")

            assert isinstance(result, dict)
            assert result["result"] == []
            assert "hint" in result
            assert "fewer keywords" in result["hint"]

    @pytest.mark.asyncio
    async def test_fts_with_results_returns_list(self):
        """FTS5 path returns plain list when results found."""
        from apple_mail_mcp.index.search import SearchResult

        mock_result = SearchResult(
            id=1,
            account="acc-uuid",
            mailbox="INBOX",
            subject="Test",
            sender="a@b.com",
            content_snippet="snippet",
            date_received="2024-01-01",
            score=1.0,
        )
        mock_manager = MagicMock()
        mock_manager.has_index.return_value = True
        mock_manager.search.return_value = [mock_result]

        mock_acct_map = MagicMock()
        mock_acct_map.ensure_loaded = AsyncMock()
        mock_acct_map.uuid_to_name.return_value = "Work"

        with (
            patch("apple_mail_mcp.server._get_index_manager") as mock_get,
            patch("apple_mail_mcp.server._get_account_map") as mock_get_acct,
        ):
            mock_get.return_value = mock_manager
            mock_get_acct.return_value = mock_acct_map

            from apple_mail_mcp.server import search

            result = await search("test")

            assert isinstance(result, list)
            assert len(result) == 1
            assert result[0]["id"] == 1


class TestGetAttachmentLinksMode:
    """get_attachment with filename=None returns links."""

    @pytest.mark.asyncio
    async def test_returns_links_when_no_filename(self, tmp_path: Path):
        from apple_mail_mcp.index.disk import LinkInfo

        mock_manager = MagicMock()
        mock_manager.has_index.return_value = True
        mock_manager.find_email_path.return_value = Path("/fake/42.emlx")

        fake_links = [
            LinkInfo(url="https://example.com", text="Example"),
            LinkInfo(url="https://other.com", text="Other"),
        ]

        with (
            patch("apple_mail_mcp.server._get_index_manager") as mock_get,
            patch(
                "apple_mail_mcp.server.asyncio.to_thread",
                new_callable=AsyncMock,
            ) as mock_thread,
        ):
            mock_get.return_value = mock_manager
            mock_thread.return_value = fake_links

            from apple_mail_mcp.server import get_attachment

            result = await get_attachment(42)

            assert "links" in result
            assert len(result["links"]) == 2
            assert result["links"][0]["url"] == "https://example.com"
            assert result["links"][0]["text"] == "Example"
            assert "file_path" not in result


class TestIndexStatusTool:
    """get_index_status(): the agent-facing diagnostics tool."""

    def _mgr(self, *, building=False, has_index=True, indexed=100, err=None):
        m = MagicMock()
        m.is_building.return_value = building
        m.write_lock_held.return_value = False
        m.has_index.return_value = has_index
        m.indexed_email_count.return_value = indexed
        m.cached_disk_count.return_value = None
        m.build_progress.return_value = None
        m.last_error = err
        return m

    @pytest.mark.asyncio
    async def test_reports_missing_full_disk_access(self):
        mgr = self._mgr(has_index=False, indexed=0)
        with (
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch(
                "apple_mail_mcp.index.disk.find_mail_directory",
                side_effect=PermissionError("denied"),
            ),
        ):
            from apple_mail_mcp.server import get_index_status

            r = await get_index_status()

        assert r["mail_dir_accessible"] is False
        assert "Full Disk Access" in r["problem"]
        assert r["state"] == "absent"

    @pytest.mark.asyncio
    async def test_state_building(self, tmp_path):
        mgr = self._mgr(building=True, indexed=42)
        with (
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch(
                "apple_mail_mcp.index.disk.find_mail_directory",
                return_value=tmp_path,
            ),
        ):
            from apple_mail_mcp.server import get_index_status

            r = await get_index_status()

        assert r["state"] == "building"
        assert r["indexed_emails"] == 42
        # Building is a transient state, not a fault: reported as a
        # note plus "wait and re-check" steps.
        assert "problem" not in r
        assert "progress" in r["note"].lower()
        assert any("again" in s.lower() for s in r["next_steps"])

    @pytest.mark.asyncio
    async def test_state_empty_when_db_has_no_rows(self, tmp_path):
        mgr = self._mgr(indexed=0)
        with (
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch(
                "apple_mail_mcp.index.disk.find_mail_directory",
                return_value=tmp_path,
            ),
        ):
            from apple_mail_mcp.server import get_index_status

            r = await get_index_status()

        assert r["state"] == "empty"

    @pytest.mark.asyncio
    async def test_ready_reports_progress_percent(self, tmp_path):
        mgr = self._mgr(indexed=50)
        stats = MagicMock()
        stats.disk_email_count = 200
        stats.mailbox_count = 3
        stats.attachment_count = 7
        stats.db_size_mb = 1.234
        stats.failed_jobs_count = 0
        stats.excluded_accounts = []
        stats.last_sync = None
        stats.staleness_hours = None
        mgr.get_stats.return_value = stats

        with (
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch(
                "apple_mail_mcp.index.disk.find_mail_directory",
                return_value=tmp_path,
            ),
        ):
            from apple_mail_mcp.server import get_index_status

            r = await get_index_status()

        assert r["state"] == "ready"
        assert r["progress_percent"] == 25.0
        assert "problem" not in r

    @pytest.mark.asyncio
    async def test_surfaces_last_error(self, tmp_path):
        mgr = self._mgr(indexed=0, err="PermissionError: nope")
        with (
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch(
                "apple_mail_mcp.index.disk.find_mail_directory",
                return_value=tmp_path,
            ),
        ):
            from apple_mail_mcp.server import get_index_status

            r = await get_index_status()

        assert r["last_error"] == "PermissionError: nope"


class TestIndexModes:
    """Both supported setups: automatic (FDA) and manual (no FDA)."""

    def _mgr(self, *, indexed, building=False, has_index=True):
        m = MagicMock()
        m.is_building.return_value = building
        m.write_lock_held.return_value = False
        m.has_index.return_value = has_index
        m.indexed_email_count.return_value = indexed
        m.last_error = None
        return m

    @pytest.mark.asyncio
    async def test_manual_mode_with_prebuilt_index_is_not_a_problem(self):
        """No FDA + index built elsewhere = working setup, not an error."""
        mgr = self._mgr(indexed=500)
        with (
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch(
                "apple_mail_mcp.index.disk.find_mail_directory",
                side_effect=PermissionError("no FDA"),
            ),
        ):
            from apple_mail_mcp.server import get_index_status

            r = await get_index_status()

        assert r["state"] == "ready"
        assert "problem" not in r  # not broken — just a different mode
        assert "note" in r
        assert "apple-mail-mcp index" in " ".join(r["next_steps"])

    @pytest.mark.asyncio
    async def test_no_fda_and_no_index_offers_both_paths(self):
        mgr = self._mgr(indexed=0, has_index=False)
        with (
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch(
                "apple_mail_mcp.index.disk.find_mail_directory",
                side_effect=PermissionError("no FDA"),
            ),
        ):
            from apple_mail_mcp.server import get_index_status

            r = await get_index_status()

        steps = " ".join(r["next_steps"])
        assert "Full Disk Access" in r["problem"] or "Full Disk Access" in steps
        assert "apple-mail-mcp index" in steps  # both options named

    @pytest.mark.asyncio
    async def test_index_mode_reflects_auto_build_setting(
        self, tmp_path, monkeypatch
    ):
        mgr = self._mgr(indexed=0, has_index=False)
        monkeypatch.setenv("APPLE_MAIL_INDEX_AUTO_BUILD", "false")
        with (
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch(
                "apple_mail_mcp.index.disk.find_mail_directory",
                return_value=tmp_path,
            ),
        ):
            from apple_mail_mcp.server import get_index_status

            r = await get_index_status()

        assert r["index_mode"] == "manual"
        # Manual mode must not promise an automatic build.
        assert "builds automatically" not in r["problem"]
        assert "apple-mail-mcp index" in " ".join(r["next_steps"])

    @pytest.mark.asyncio
    async def test_automatic_mode_mentions_self_build(
        self, tmp_path, monkeypatch
    ):
        mgr = self._mgr(indexed=0, has_index=False)
        monkeypatch.setenv("APPLE_MAIL_INDEX_AUTO_BUILD", "true")
        with (
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch(
                "apple_mail_mcp.index.disk.find_mail_directory",
                return_value=tmp_path,
            ),
        ):
            from apple_mail_mcp.server import get_index_status

            r = await get_index_status()

        assert r["index_mode"] == "automatic"
        assert "automatically" in r["problem"]
        assert any("Cmd-Q" in s for s in r["next_steps"])


class TestAgentGuidance:
    """The status tool must hand the assistant usable instructions."""

    def _mgr(self, *, indexed, building=False, has_index=True):
        m = MagicMock()
        m.is_building.return_value = building
        m.write_lock_held.return_value = False
        m.has_index.return_value = has_index
        m.indexed_email_count.return_value = indexed
        m.last_error = None
        return m

    async def _status(self, mgr, *, accessible, tmp_path=None):
        patches = [
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr)
        ]
        if accessible:
            patches.append(
                patch(
                    "apple_mail_mcp.index.disk.find_mail_directory",
                    return_value=tmp_path,
                )
            )
        else:
            patches.append(
                patch(
                    "apple_mail_mcp.index.disk.find_mail_directory",
                    side_effect=PermissionError("no FDA"),
                )
            )
        from apple_mail_mcp.server import get_index_status

        for p in patches:
            p.start()
        try:
            return await get_index_status()
        finally:
            for p in patches:
                p.stop()

    @pytest.mark.asyncio
    async def test_always_returns_user_message_and_instructions(self, tmp_path):
        r = await self._status(
            self._mgr(indexed=10), accessible=True, tmp_path=tmp_path
        )
        assert r["user_message"]
        assert "next_steps" in r or r["state"] == "ready"
        assert "assistant_instructions" in r

    @pytest.mark.asyncio
    async def test_no_fda_gives_gui_first_steps(self):
        r = await self._status(
            self._mgr(indexed=0, has_index=False), accessible=False
        )
        steps = r["next_steps"]
        # The first instruction must be a GUI action, not a command.
        assert "System Settings" in steps[0]
        assert any("Full Disk Access" in s for s in steps)

    @pytest.mark.asyncio
    async def test_telemetry_fields_present(self, tmp_path):
        r = await self._status(
            self._mgr(indexed=5), accessible=True, tmp_path=tmp_path
        )
        for key in (
            "install_mode",  # fork-only
            "server_version",
            "index_mode",
            "write_tools_enabled",
            "index_command",
        ):
            assert key in r, key

    # fork-only:start — _install_mode only exists in this fork
    def test_index_command_matches_install_mode(self, monkeypatch):
        from apple_mail_mcp.server import _index_command, _install_mode

        monkeypatch.delenv("APPLE_MAIL_MCP_LAUNCHER", raising=False)
        assert _install_mode() == "cli"
        assert _index_command() == "apple-mail-mcp index --verbose"

        monkeypatch.setenv("APPLE_MAIL_MCP_LAUNCHER", "mcpb")
        monkeypatch.delenv("APPLE_MAIL_MCP_REF", raising=False)
        assert _install_mode() == "bundle"
        # Bundle users have no CLI on PATH -> must go through uvx.
        assert _index_command().startswith("uvx --from apple-mail-mcp")

        monkeypatch.setenv("APPLE_MAIL_MCP_REF", "git+https://example/x@main")
        assert "git+https://example/x@main" in _index_command()

    # fork-only:end

    def test_index_command_has_no_hardcoded_fork(self, monkeypatch):
        """Upstream-safety: no fork URL baked into the source."""
        from pathlib import Path

        import apple_mail_mcp.server as mod

        assert "iret77" not in Path(mod.__file__).read_text()


class TestRefreshIndex:
    """refresh_index(): on-demand index update."""

    def _mgr(self, *, building=False, usable=True, changes=0, err=None):
        m = MagicMock()
        m.is_building.return_value = building
        m.write_lock_held.return_value = False
        m.write_lock_held.return_value = False
        m.has_usable_index.return_value = usable
        m.sync_updates.return_value = changes
        m.last_error = err
        return m

    @pytest.mark.asyncio
    async def test_incremental_sync_reports_changes(self):
        mgr = self._mgr(changes=7)
        with patch(
            "apple_mail_mcp.server._get_index_manager", return_value=mgr
        ):
            from apple_mail_mcp.server import refresh_index

            r = await refresh_index()

        assert r["status"] == "completed"
        assert r["changes"] == 7
        mgr.sync_updates.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_changes_is_still_success(self):
        mgr = self._mgr(changes=0)
        with patch(
            "apple_mail_mcp.server._get_index_manager", return_value=mgr
        ):
            from apple_mail_mcp.server import refresh_index

            r = await refresh_index()

        assert r["status"] == "completed"
        assert "up to date" in r["message"]

    @pytest.mark.asyncio
    async def test_permission_failure_is_not_reported_as_success(self):
        """sync_updates returns 0 on FDA failure — must not read as OK."""
        mgr = self._mgr(changes=0, err="PermissionError: denied")
        with patch(
            "apple_mail_mcp.server._get_index_manager", return_value=mgr
        ):
            from apple_mail_mcp.server import refresh_index

            r = await refresh_index()

        assert r["status"] == "failed"
        assert r["error"] == "PermissionError: denied"
        assert r["next_steps"]

    @pytest.mark.asyncio
    async def test_refuses_while_a_build_runs(self):
        mgr = self._mgr(building=True)
        with patch(
            "apple_mail_mcp.server._get_index_manager", return_value=mgr
        ):
            from apple_mail_mcp.server import refresh_index

            r = await refresh_index()

        assert r["status"] == "already_running"
        mgr.sync_updates.assert_not_called()

    @pytest.mark.asyncio
    async def test_full_rebuild_runs_detached(self):
        mgr = self._mgr()
        done = threading.Event()
        mgr.build_from_disk.side_effect = lambda *a, **k: done.set()

        with patch(
            "apple_mail_mcp.server._get_index_manager", return_value=mgr
        ):
            from apple_mail_mcp.server import refresh_index

            r = await refresh_index(full=True)

        assert r["status"] == "started"  # returns without waiting
        assert done.wait(timeout=5), "build thread did not run"
        mgr.sync_updates.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_index_triggers_build_not_sync(self):
        mgr = self._mgr(usable=False)
        done = threading.Event()
        mgr.build_from_disk.side_effect = lambda *a, **k: done.set()

        with patch(
            "apple_mail_mcp.server._get_index_manager", return_value=mgr
        ):
            from apple_mail_mcp.server import refresh_index

            r = await refresh_index()

        assert r["status"] == "started"
        assert done.wait(timeout=5)
        mgr.sync_updates.assert_not_called()

    @pytest.mark.asyncio
    async def test_allowed_in_read_only_mode(self):
        """Refreshing the local index is not a mail mutation."""
        mgr = self._mgr(changes=1)
        with (
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch(
                "apple_mail_mcp.server.get_read_only_mode", return_value=True
            ),
        ):
            from apple_mail_mcp.server import refresh_index

            r = await refresh_index()

        assert r["status"] == "completed"


class TestLiveFlagOverlay:
    """get_email must not report stale flag state from the .emlx footer."""

    @pytest.mark.asyncio
    async def test_overlay_replaces_stale_disk_values(self, tmp_path):
        from apple_mail_mcp.server import _overlay_live_flags

        env = tmp_path / "Envelope Index"
        env.write_bytes(b"")
        result = {"read": True, "flagged": True}  # stale footer values

        with (
            patch(
                "apple_mail_mcp.index.disk.find_mail_directory",
                return_value=tmp_path,
            ),
            patch(
                "apple_mail_mcp.index.envelope_direct.envelope_index_path",
                return_value=env,
            ),
            patch(
                "apple_mail_mcp.index.envelope_direct.fetch_message_flags",
                return_value=(False, False),  # user cleared them in Mail
            ),
        ):
            await _overlay_live_flags(result, 42)

        assert result["flagged"] is False
        assert result["read"] is False

    @pytest.mark.asyncio
    async def test_unknown_message_leaves_values_untouched(self, tmp_path):
        from apple_mail_mcp.server import _overlay_live_flags

        env = tmp_path / "Envelope Index"
        env.write_bytes(b"")
        result = {"read": True, "flagged": True}

        with (
            patch(
                "apple_mail_mcp.index.disk.find_mail_directory",
                return_value=tmp_path,
            ),
            patch(
                "apple_mail_mcp.index.envelope_direct.envelope_index_path",
                return_value=env,
            ),
            patch(
                "apple_mail_mcp.index.envelope_direct.fetch_message_flags",
                return_value=None,
            ),
        ):
            await _overlay_live_flags(result, 42)

        assert result["flagged"] is True

    @pytest.mark.asyncio
    async def test_failure_is_non_fatal(self):
        """No Mail access (or any error) must not break get_email."""
        from apple_mail_mcp.server import _overlay_live_flags

        result = {"read": False, "flagged": True}
        with patch(
            "apple_mail_mcp.index.disk.find_mail_directory",
            side_effect=PermissionError("no FDA"),
        ):
            await _overlay_live_flags(result, 42)

        assert result == {"read": False, "flagged": True}

    def test_fetch_message_flags_reads_live_columns(self, tmp_path):
        """Against a real SQLite file shaped like Apple's index."""
        import sqlite3

        from apple_mail_mcp.index.envelope_direct import fetch_message_flags

        env = tmp_path / "Envelope Index"
        conn = sqlite3.connect(env)
        conn.execute("CREATE TABLE messages (read INTEGER, flagged INTEGER)")
        conn.execute("INSERT INTO messages (read, flagged) VALUES (1, 0)")
        conn.commit()
        rowid = conn.execute("SELECT ROWID FROM messages").fetchone()[0]
        conn.close()

        assert fetch_message_flags(env, rowid) == (True, False)
        assert fetch_message_flags(env, 9999) is None


class TestStableMessageIdentity:
    """RFC822 Message-ID as the identity that survives moves."""

    def test_schema_v6_column_and_index_exist(self, temp_db):
        cols = {r[1] for r in temp_db.execute("PRAGMA table_info(emails)")}
        assert "rfc822_message_id" in cols
        idx = {
            r[0]
            for r in temp_db.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        }
        assert "idx_emails_rfc822" in idx

    def test_email_to_row_carries_the_header(self):
        from apple_mail_mcp.index.schema import email_to_row

        row = email_to_row(
            {"id": 1, "message_id_header": "<abc@example.com>"}, "acct", "INBOX"
        )
        assert "<abc@example.com>" in row

    def test_missing_header_stored_as_null_not_empty(self):
        from apple_mail_mcp.index.schema import email_to_row

        row = email_to_row({"id": 1, "message_id_header": ""}, "a", "INBOX")
        assert row[-2] is None  # NULL, so the index stays selective

    def test_migration_from_v5_adds_column(self, tmp_path):
        """A v5 database must migrate in place, not be discarded."""
        import sqlite3

        from apple_mail_mcp.index.schema import (
            SCHEMA_VERSION,
            init_database,
        )

        db = tmp_path / "old.db"
        conn = sqlite3.connect(db)
        conn.executescript("""
            CREATE TABLE emails (
                rowid INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id INTEGER NOT NULL,
                account TEXT NOT NULL,
                mailbox TEXT NOT NULL,
                subject TEXT, sender TEXT, content TEXT,
                date_received TEXT, emlx_path TEXT,
                attachment_count INTEGER DEFAULT 0,
                indexed_at TEXT,
                UNIQUE(account, mailbox, message_id)
            );
            CREATE TABLE schema_version (version INTEGER);
            INSERT INTO schema_version (version) VALUES (5);
            INSERT INTO emails (message_id, account, mailbox, subject)
                VALUES (42, 'acct', 'INBOX', 'kept');
        """)
        conn.commit()
        conn.close()

        migrated = init_database(db)
        try:
            cols = {r[1] for r in migrated.execute("PRAGMA table_info(emails)")}
            assert "rfc822_message_id" in cols
            # Pre-existing data survives; the new column is simply NULL.
            row = migrated.execute(
                "SELECT subject, rfc822_message_id FROM emails "
                "WHERE message_id = 42"
            ).fetchone()
            assert row[0] == "kept"
            assert row[1] is None
            version = migrated.execute(
                "SELECT version FROM schema_version"
            ).fetchone()[0]
            assert version == SCHEMA_VERSION
        finally:
            migrated.close()

    def test_lookup_roundtrip(self, temp_db_path):
        from apple_mail_mcp.index import IndexManager
        from apple_mail_mcp.index.schema import INSERT_EMAIL_SQL, email_to_row

        m = IndexManager(db_path=temp_db_path)
        conn = m._get_conn()
        conn.execute(
            INSERT_EMAIL_SQL,
            email_to_row(
                {"id": 7, "message_id_header": "<stable@x>"}, "acct", "INBOX"
            ),
        )
        conn.commit()

        assert m.get_rfc822_id(7) == "<stable@x>"
        assert m.find_by_rfc822("<stable@x>") == [("acct", "INBOX", 7)]
        assert m.get_rfc822_id(999) is None
        assert m.find_by_rfc822("<nope@x>") == []

    def test_find_by_rfc822_returns_every_copy(self, temp_db_path):
        """The same mail can sit in several mailboxes — return all."""
        from apple_mail_mcp.index import IndexManager
        from apple_mail_mcp.index.schema import INSERT_EMAIL_SQL, email_to_row

        m = IndexManager(db_path=temp_db_path)
        conn = m._get_conn()
        for mid, mbox in ((1, "INBOX"), (2, "Archive")):
            conn.execute(
                INSERT_EMAIL_SQL,
                email_to_row(
                    {"id": mid, "message_id_header": "<dup@x>"}, "acct", mbox
                ),
            )
        conn.commit()

        found = m.find_by_rfc822("<dup@x>")
        assert len(found) == 2
        assert {f[1] for f in found} == {"INBOX", "Archive"}


class TestRefreshIndexTellsTheTruth:
    """ "started" must mean started — the bug that made a refused
    rebuild look like a running one."""

    def _mgr(self, *, busy=False):
        m = MagicMock()
        m.is_building.return_value = False
        m.write_lock_held.return_value = busy
        m.has_usable_index.return_value = True
        m.last_error = None
        return m

    @pytest.mark.asyncio
    async def test_refused_build_is_not_reported_as_started(self):
        from apple_mail_mcp.index.manager import IndexBusyError

        mgr = self._mgr()
        mgr.build_from_disk.side_effect = IndexBusyError("already running")

        with patch(
            "apple_mail_mcp.server._get_index_manager", return_value=mgr
        ):
            from apple_mail_mcp.server import refresh_index

            r = await refresh_index(full=True)

        assert r["status"] == "already_running"
        assert "rebuild" in r["message"].lower()

    @pytest.mark.asyncio
    async def test_crashing_build_is_reported_as_failed(self):
        mgr = self._mgr()
        mgr.build_from_disk.side_effect = OSError("disk gone")

        with patch(
            "apple_mail_mcp.server._get_index_manager", return_value=mgr
        ):
            from apple_mail_mcp.server import refresh_index

            r = await refresh_index(full=True)

        assert r["status"] == "failed"
        assert "OSError" in r["error"]

    @pytest.mark.asyncio
    async def test_real_start_is_confirmed_via_on_started(self):
        mgr = self._mgr()

        def build(progress_callback=None, on_started=None):
            if on_started:
                on_started()

        mgr.build_from_disk.side_effect = build

        with patch(
            "apple_mail_mcp.server._get_index_manager", return_value=mgr
        ):
            from apple_mail_mcp.server import refresh_index

            r = await refresh_index(full=True)

        assert r["status"] == "started"

    @pytest.mark.asyncio
    async def test_busy_lock_short_circuits_before_spawning(self):
        mgr = self._mgr(busy=True)

        with patch(
            "apple_mail_mcp.server._get_index_manager", return_value=mgr
        ):
            from apple_mail_mcp.server import refresh_index

            r = await refresh_index(full=True)

        assert r["status"] == "already_running"
        mgr.build_from_disk.assert_not_called()


class TestDiagnosticsAreReachable:
    """The extension's stderr is a black hole: everything a user needs
    to diagnose must come back through the status tool."""

    def test_every_build_failure_lands_in_last_error(
        self, temp_db_path, tmp_path
    ):
        """Only the unreadable-mail-dir case used to set last_error, so
        any other crash left the status reporting "no errors"."""
        from apple_mail_mcp.index.manager import IndexManager

        m = IndexManager(db_path=temp_db_path)
        with (
            patch(
                "apple_mail_mcp.index.disk.find_mail_directory",
                return_value=tmp_path,
            ),
            patch(
                "apple_mail_mcp.index.disk.scan_all_emails",
                side_effect=RuntimeError("boom mid-scan"),
            ),
            pytest.raises(RuntimeError),
        ):
            m.build_from_disk()

        assert "RuntimeError" in (m.last_error or "")
        assert "boom mid-scan" in m.last_error

    def test_failure_is_also_in_the_event_ring(self, temp_db_path, tmp_path):
        from apple_mail_mcp.index.manager import IndexManager

        m = IndexManager(db_path=temp_db_path)
        with (
            patch(
                "apple_mail_mcp.index.disk.find_mail_directory",
                return_value=tmp_path,
            ),
            patch(
                "apple_mail_mcp.index.disk.scan_all_emails",
                side_effect=RuntimeError("boom"),
            ),
            pytest.raises(RuntimeError),
        ):
            m.build_from_disk()

        events = m.recent_events()
        assert events[0]["level"] == "error"
        assert "failed" in events[0]["message"].lower()
        # The start is recorded too, so "did it even begin?" is answerable.
        assert any("started" in e["message"].lower() for e in events)

    def test_events_are_newest_first_and_bounded(self, temp_db_path):
        from apple_mail_mcp.index.manager import MAX_EVENTS, IndexManager

        m = IndexManager(db_path=temp_db_path)
        for i in range(MAX_EVENTS + 20):
            m.record_event("info", f"event {i}")

        events = m.recent_events(limit=5)
        assert len(events) == 5
        assert events[0]["message"] == f"event {MAX_EVENTS + 19}"
        assert len(m._events) == MAX_EVENTS  # ring, not a leak

    def test_recording_never_raises(self, temp_db_path):
        """Diagnostics must not be able to break what they describe."""
        from apple_mail_mcp.index.manager import IndexManager

        m = IndexManager(db_path=temp_db_path)

        class Unprintable:
            def __repr__(self):
                raise ValueError("nope")

        m.record_event("info", "x", weird=Unprintable())  # must not raise

    @pytest.mark.asyncio
    async def test_status_carries_events_and_build_identity(self, tmp_path):
        mgr = MagicMock()
        mgr.is_building.return_value = False
        mgr.write_lock_held.return_value = False
        mgr.last_error = None
        mgr.has_index.return_value = True
        mgr.count_email_locations.return_value = 1
        mgr.is_building.return_value = False
        mgr.write_lock_held.return_value = False
        mgr.last_error = None
        mgr.has_usable_index.return_value = True
        mgr.indexed_email_count.return_value = 10
        mgr.count_skipped_too_large.return_value = 0
        mgr.count_without_stable_id.return_value = 0
        mgr.build_progress.return_value = None
        mgr.last_error = None
        mgr.recent_events.return_value = [
            {
                "at": "2026-07-26T17:20:00",
                "level": "error",
                "message": "Index build failed",
                "error": "OSError: nope",
            }
        ]
        stats = MagicMock()
        stats.disk_email_count = 10
        stats.mailbox_count = 1
        stats.attachment_count = 0
        stats.db_size_mb = 1.0
        stats.failed_jobs_count = 0
        stats.excluded_accounts = []
        stats.last_sync = None
        stats.staleness_hours = None
        mgr.get_stats.return_value = stats

        with (
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch(
                "apple_mail_mcp.index.disk.find_mail_directory",
                return_value=tmp_path,
            ),
        ):
            from apple_mail_mcp.server import get_index_status

            r = await get_index_status()

        assert r["recent_events"][0]["message"] == "Index build failed"
        assert r["server_revision"]  # fork-only
        assert "log_file" in r
        assert "source_ref" in r  # fork-only

    def test_log_path_is_configurable_and_disableable(self, monkeypatch):
        from apple_mail_mcp.config import get_log_path

        monkeypatch.delenv("APPLE_MAIL_LOG_PATH", raising=False)
        assert get_log_path().name == "server.log"

        monkeypatch.setenv("APPLE_MAIL_LOG_PATH", "/tmp/custom.log")
        assert str(get_log_path()) == "/tmp/custom.log"

        # Disabled must be an explicit None: Path("") normalizes to
        # ".", which is truthy, so the old guard never fired.
        monkeypatch.setenv("APPLE_MAIL_LOG_PATH", "")
        assert get_log_path() is None

    def test_file_log_is_written_and_owner_only(self, tmp_path, monkeypatch):
        import logging

        from apple_mail_mcp.cli import _setup_file_logging

        target = tmp_path / "sub" / "server.log"
        monkeypatch.setenv("APPLE_MAIL_LOG_PATH", str(target))
        path = _setup_file_logging()
        try:
            logging.getLogger("apple_mail_mcp.test").info("hello from test")
            for h in logging.getLogger("apple_mail_mcp").handlers:
                h.flush()

            assert path == target
            assert target.exists()
            assert "hello from test" in target.read_text()
            assert oct(target.stat().st_mode)[-3:] == "600"
        finally:
            root = logging.getLogger("apple_mail_mcp")
            for h in list(root.handlers):
                h.close()
                root.removeHandler(h)


class TestDiagnosticsDoNotLie:
    """Regressions for the review of the diagnostics pass itself."""

    def test_disabled_logging_is_reported_as_disabled(self, monkeypatch):
        from apple_mail_mcp.cli import _setup_file_logging
        from apple_mail_mcp.server import _log_file_path

        monkeypatch.setenv("APPLE_MAIL_LOG_PATH", "")
        assert _setup_file_logging() is None
        assert _log_file_path() == "(disabled)"

    def test_rotated_log_stays_owner_only(self, tmp_path, monkeypatch):
        """chmod once is not enough: on rollover the handler reopens the
        path with plain open(), i.e. 0644 under the usual umask."""
        import logging

        from apple_mail_mcp.cli import _setup_file_logging

        target = tmp_path / "server.log"
        monkeypatch.setenv("APPLE_MAIL_LOG_PATH", str(target))
        _setup_file_logging()
        root = logging.getLogger("apple_mail_mcp")
        try:
            handler = root.handlers[-1]
            handler.maxBytes = 200  # force a rollover
            log = logging.getLogger("apple_mail_mcp.rotate_test")
            for i in range(50):
                log.info("padding line %d %s", i, "x" * 40)
            handler.flush()

            assert (tmp_path / "server.log.1").exists(), "no rollover"
            for f in (target, tmp_path / "server.log.1"):
                assert oct(f.stat().st_mode)[-3:] == "600", f
        finally:
            for h in list(root.handlers):
                h.close()
                root.removeHandler(h)

    def test_failed_sync_is_not_topped_by_a_success_event(self, temp_db_path):
        """The FDA path returns 0 instead of raising, so "Sync finished"
        landed on top of the ring the model is told to quote."""
        from apple_mail_mcp.index.manager import IndexManager

        m = IndexManager(db_path=temp_db_path)
        with patch(
            "apple_mail_mcp.index.disk.find_mail_directory",
            side_effect=PermissionError("no FDA"),
        ):
            assert m.sync_updates() == 0

        top = m.recent_events()[0]
        assert top["level"] == "error"
        assert "finished" not in top["message"].lower()
        assert m.last_error and "PermissionError" in m.last_error

    def test_swallowed_finalize_error_is_not_reported_as_success(
        self, temp_db_path, tmp_path
    ):
        """Rows exist but FTS is empty: body search is permanently
        broken, so "finished" with last_error None hides it completely."""
        import sqlite3 as _sqlite3

        from apple_mail_mcp.index import manager as mgr_mod
        from apple_mail_mcp.index.manager import IndexManager

        m = IndexManager(db_path=temp_db_path)
        emails = [
            {
                "id": i,
                "account": "acct",
                "mailbox": "INBOX",
                "subject": "s",
                "sender": "a@b",
                "content": "c",
                "date_received": "2026-01-01",
                "emlx_path": f"/p/{i}.emlx",
                "message_id_header": f"<{i}@x>",
                "attachments": [],
            }
            for i in range(2)
        ]

        with (
            patch(
                "apple_mail_mcp.index.disk.find_mail_directory",
                return_value=tmp_path,
            ),
            patch(
                "apple_mail_mcp.index.disk.scan_all_emails",
                return_value=iter(emails),
            ),
            patch.object(
                mgr_mod,
                "rebuild_fts_index",
                side_effect=_sqlite3.OperationalError("disk full"),
            ),
        ):
            m.build_from_disk()

        assert m.last_error and "disk full" in m.last_error
        top = m.recent_events()[0]
        assert top["level"] == "error"
        assert "incomplete" in top["message"].lower()

    def test_start_event_exists_even_without_file_logging(
        self, temp_db_path, monkeypatch
    ):
        from apple_mail_mcp.index.manager import IndexManager

        m = IndexManager(db_path=temp_db_path)
        m.record_event("info", "Server started", log="file logging unavailable")
        assert m.recent_events()[0]["message"] == "Server started"

    def test_event_fields_are_always_serializable(self, temp_db_path):
        from apple_mail_mcp.index.manager import IndexManager

        class Weird:
            def __str__(self):
                return "weird-value"

        m = IndexManager(db_path=temp_db_path)
        m.record_event("info", "x", obj=Weird(), n=5)
        # Must survive the JSON round-trip the MCP response performs.
        json.dumps(m.recent_events())


class TestRebuildIsDiscoverable:
    """ "Rebuild the mail index" must route here, not to Mail.app's own
    Mailbox > Rebuild — a live session sent the user there instead."""

    @pytest.mark.asyncio
    async def test_description_claims_the_rebuild_vocabulary(self):
        from apple_mail_mcp.server import mcp

        desc = (await mcp.get_tool("refresh_index")).description.lower()
        for phrase in ("rebuild", "from scratch", "re-index", "recreate"):
            assert phrase in desc, phrase

    @pytest.mark.asyncio
    async def test_description_disambiguates_from_apple_mails_index(self):
        from apple_mail_mcp.server import mcp

        # Normalize: the docstring is line-wrapped, so phrases span
        # newlines in the source.
        desc = " ".join(
            (await mcp.get_tool("refresh_index")).description.split()
        ).lower()
        assert "not apple mail's own envelope index" in desc
        assert "mailbox > rebuild" in desc
        assert "never send the user there" in desc


class TestTimestampsAreLocal:
    """Mail.app showed 14:54 while the tool said 12:54 — stored UTC was
    handed to the reader unconverted."""

    def test_utc_is_converted_to_the_running_system_zone(self, monkeypatch):
        import time as _time

        from apple_mail_mcp.server import to_local_iso

        # Two different zones: the conversion must follow the system,
        # never a value baked into the code.
        for tz, expected_hour in (("Europe/Berlin", 14), ("UTC", 12)):
            monkeypatch.setenv("TZ", tz)
            _time.tzset()
            out = to_local_iso("2026-07-27T12:54:00+00:00")
            assert out is not None
            assert int(out[11:13]) == expected_hour, (tz, out)

    def test_naive_values_are_read_as_utc(self, monkeypatch):
        import time as _time

        from apple_mail_mcp.server import to_local_iso

        monkeypatch.setenv("TZ", "Europe/Berlin")
        _time.tzset()
        # Everything this server writes is UTC, so a naive string must
        # not be mistaken for local time.
        assert to_local_iso("2026-07-27T12:54:00")[11:13] == "14"

    def test_unparseable_and_empty_values_survive_untouched(self):
        from apple_mail_mcp.server import to_local_iso

        for value in ("Mon, 1 Jan 2026 10:00:00 +0100", "", None, "garbage"):
            assert to_local_iso(value) == value

    def test_dst_is_honoured(self, monkeypatch):
        """A fixed offset would be wrong for half the year."""
        import time as _time

        from apple_mail_mcp.server import to_local_iso

        monkeypatch.setenv("TZ", "Europe/Berlin")
        _time.tzset()
        summer = to_local_iso("2026-07-27T12:00:00+00:00")
        winter = to_local_iso("2026-01-27T12:00:00+00:00")
        assert summer.endswith("+02:00")  # CEST
        assert winter.endswith("+01:00")  # CET

    @pytest.mark.asyncio
    async def test_get_email_reports_local_time(self):
        parsed = MagicMock()
        parsed.id = 42
        parsed.subject = "s"
        parsed.sender = "a@b"
        parsed.content = "c"
        parsed.date_received = "2026-07-27T12:54:00+00:00"
        parsed.date_sent = "2026-07-27T12:50:00+00:00"
        parsed.read = True
        parsed.flagged = False
        parsed.reply_to = ""
        parsed.message_id_header = "<x@y>"
        parsed.attachments = []

        mgr = MagicMock()
        mgr.has_index.return_value = True
        mgr.count_email_locations.return_value = 1
        mgr.is_building.return_value = False
        mgr.write_lock_held.return_value = False
        mgr.last_error = None
        mgr.has_usable_index.return_value = True
        mgr.find_email_path.return_value = MagicMock(exists=lambda: True)
        mgr.get_email_attachments.return_value = None
        amap = _mock_acct_map()

        import time as _time

        os.environ["TZ"] = "Europe/Berlin"
        _time.tzset()
        try:
            with (
                patch(
                    "apple_mail_mcp.server._get_index_manager",
                    return_value=mgr,
                ),
                patch(
                    "apple_mail_mcp.server._get_account_map",
                    return_value=amap,
                ),
                patch(
                    "apple_mail_mcp.index.disk.parse_emlx", return_value=parsed
                ),
                patch(
                    "apple_mail_mcp.server._overlay_live_flags",
                    new_callable=AsyncMock,
                ),
            ):
                from apple_mail_mcp.server import get_email

                r = await get_email(42)
        finally:
            os.environ.pop("TZ", None)
            _time.tzset()

        assert r["date_received"].startswith("2026-07-27T14:54")
        assert r["date_sent"].startswith("2026-07-27T14:50")


class TestReadsHandOutTheStableId:
    """Every listing path must offer the handle the writes want."""

    def test_search_result_carries_message_id(self):
        from apple_mail_mcp.index.search import SearchResult

        r = SearchResult(
            id=1,
            account="uuid",
            mailbox="INBOX",
            subject="s",
            sender="f",
            content_snippet="c",
            date_received="2026-01-01",
            score=1.0,
            rfc822_message_id="<x@y.com>",
        )
        assert r.rfc822_message_id == "<x@y.com>"

    def test_jxa_listings_fetch_the_header(self):
        """The standard property set must include the stable id."""
        from apple_mail_mcp.builders import PROPERTY_SETS, QueryBuilder

        assert "message_id" in PROPERTY_SETS["standard"]
        script = QueryBuilder().from_mailbox("Work", "INBOX").build()
        assert "messageId" in script

    def test_fts_sql_selects_the_stable_column(self):
        import inspect

        from apple_mail_mcp.index import search as search_mod

        src = inspect.getsource(search_mod)
        # Both the ranked and the highlighted query, plus attachments.
        assert src.count("rfc822_message_id") >= 3


class TestReadingByMessageIdIsVerified:
    """A stale index row must never hand back a different message.

    The header maps to a ROWID via the index, and that mapping can be
    out of date — the ROWID may by then belong to another message in
    that mailbox. Reads therefore check what came back.
    """

    @pytest.mark.asyncio
    async def test_get_email_returns_the_matching_message(self):
        mgr = MagicMock()
        mgr.has_index.return_value = True
        mgr.count_email_locations.return_value = 1
        mgr.is_building.return_value = False
        mgr.write_lock_held.return_value = False
        mgr.last_error = None
        mgr.has_usable_index.return_value = True
        mgr.find_by_rfc822.return_value = [("uuid-work", "Archiv", 42)]
        amap = _mock_acct_map()

        async def fake_by_id(rowid, account=None, mailbox=None):
            return {"id": rowid, "message_id": "<a@x.com>", "subject": "hi"}

        with (
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch("apple_mail_mcp.server._get_account_map", return_value=amap),
            patch(
                "apple_mail_mcp.server._get_email_by_id",
                side_effect=fake_by_id,
            ),
        ):
            from apple_mail_mcp.server import get_email

            result = await get_email("<a@x.com>")

        assert result["subject"] == "hi"

    @pytest.mark.asyncio
    async def test_stale_row_is_rejected_not_returned(self):
        """The ROWID now holds someone else's mail → error, not that mail."""
        mgr = MagicMock()
        mgr.has_index.return_value = True
        mgr.count_email_locations.return_value = 1
        mgr.is_building.return_value = False
        mgr.write_lock_held.return_value = False
        mgr.last_error = None
        mgr.has_usable_index.return_value = True
        mgr.find_by_rfc822.return_value = [("uuid-work", "INBOX", 42)]
        amap = _mock_acct_map()

        async def fake_by_id(rowid, account=None, mailbox=None):
            return {
                "id": rowid,
                "message_id": "<somebody-else@x.com>",
                "subject": "NOT the requested mail",
            }

        with (
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch("apple_mail_mcp.server._get_account_map", return_value=amap),
            patch(
                "apple_mail_mcp.server._get_email_by_id",
                side_effect=fake_by_id,
            ),
        ):
            from apple_mail_mcp.server import get_email

            with pytest.raises(ValueError, match="stale"):
                await get_email("<a@x.com>")

    @pytest.mark.asyncio
    async def test_second_candidate_is_tried(self):
        """Two indexed copies: the stale one must not end the search."""
        mgr = MagicMock()
        mgr.has_index.return_value = True
        mgr.count_email_locations.return_value = 1
        mgr.is_building.return_value = False
        mgr.write_lock_held.return_value = False
        mgr.last_error = None
        mgr.has_usable_index.return_value = True
        mgr.find_by_rfc822.return_value = [
            ("uuid-work", "INBOX", 42),
            ("uuid-work", "Archiv", 77),
        ]
        amap = _mock_acct_map()

        async def fake_by_id(rowid, account=None, mailbox=None):
            header = "<a@x.com>" if rowid == 77 else "<other@x.com>"
            return {"id": rowid, "message_id": header, "subject": "found"}

        with (
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch("apple_mail_mcp.server._get_account_map", return_value=amap),
            patch(
                "apple_mail_mcp.server._get_email_by_id",
                side_effect=fake_by_id,
            ),
        ):
            from apple_mail_mcp.server import get_email

            result = await get_email("<a@x.com>")

        assert result["id"] == 77

    @pytest.mark.asyncio
    async def test_hidden_account_copy_is_never_read(self):
        mgr = MagicMock()
        mgr.has_index.return_value = True
        mgr.count_email_locations.return_value = 1
        mgr.is_building.return_value = False
        mgr.write_lock_held.return_value = False
        mgr.last_error = None
        mgr.has_usable_index.return_value = True
        mgr.find_by_rfc822.return_value = [("uuid-secret", "INBOX", 1)]
        amap = _mock_acct_map(uuid_to_name="Secret")
        calls = []

        async def fake_by_id(rowid, account=None, mailbox=None):
            calls.append(rowid)
            return {"id": rowid, "message_id": "<a@x.com>"}

        with (
            patch(
                "apple_mail_mcp.server._excluded_account_names",
                return_value={"Secret"},
            ),
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch("apple_mail_mcp.server._get_account_map", return_value=amap),
            patch(
                "apple_mail_mcp.server._get_email_by_id",
                side_effect=fake_by_id,
            ),
        ):
            from apple_mail_mcp.server import get_email

            with pytest.raises(ValueError):
                await get_email("<a@x.com>")

        assert calls == []

    @pytest.mark.asyncio
    async def test_attachment_path_verifies_the_header(self):
        """A stale row must not expose another message's attachments."""
        from types import SimpleNamespace

        mgr = MagicMock()
        mgr.has_index.return_value = True
        mgr.count_email_locations.return_value = 1
        mgr.is_building.return_value = False
        mgr.write_lock_held.return_value = False
        mgr.last_error = None
        mgr.has_usable_index.return_value = True
        mgr.find_by_rfc822.return_value = [("uuid-work", "INBOX", 42)]
        mgr.find_email_path.return_value = "/tmp/wrong.emlx"
        amap = _mock_acct_map()

        with (
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch("apple_mail_mcp.server._get_account_map", return_value=amap),
            patch(
                "apple_mail_mcp.server._excluded_account_uuids",
                AsyncMock(return_value=set()),
            ),
            patch(
                "apple_mail_mcp.index.disk.parse_emlx",
                return_value=SimpleNamespace(
                    message_id_header="<somebody-else@x.com>"
                ),
            ),
        ):
            from apple_mail_mcp.server import _resolve_emlx_path

            with pytest.raises(ValueError, match="stale"):
                await _resolve_emlx_path("<a@x.com>")


class TestUnsupportedLanguageDegradesUsefully:
    """A language we do not cover must not be a dead end.

    Nothing crashes: reads and search run off the index and never touch
    a mailbox name, writes report the failure with a reason, and a JXA
    listing says which mailboxes the account actually has — which is
    all a caller needs to retry correctly.
    """

    @pytest.mark.asyncio
    async def test_listing_names_the_real_mailboxes(self):
        async def turkish(*a, **kw):
            raise RuntimeError(
                'No mailbox matching "INBOX" (role: inbox). '
                "Available: Gelen Kutusu, Gönderilmiş, Çöp Kutusu"
            )

        mgr = MagicMock()
        mgr.has_index.return_value = False
        mgr.is_building.return_value = False
        mgr.write_lock_held.return_value = False
        mgr.last_error = None
        mgr.has_usable_index.return_value = False
        with (
            patch(
                "apple_mail_mcp.server.execute_query_async",
                side_effect=turkish,
            ),
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch(
                "apple_mail_mcp.server._resolve_visible_account",
                AsyncMock(return_value="Work"),
            ),
        ):
            from apple_mail_mcp.server import get_emails

            with pytest.raises(ValueError) as err:
                await get_emails()

        text = str(err.value)
        assert "Gelen Kutusu" in text  # the actual names are handed over
        assert "list_mailboxes" in text  # and a way to act on them
        assert "system language" in text

    @pytest.mark.asyncio
    async def test_write_reports_it_as_failed_not_as_missing_mail(self):
        mgr = _mock_index(location=("uuid-work", "INBOX"))
        amap = _mock_acct_map()

        async def jxa(script, **kw):
            return {
                "updated": [],
                "unchanged": [],
                "not_found": [],
                "failures": [
                    {
                        "target": 5,
                        "reason": (
                            "cannot open mailbox INBOX in account Work "
                            "(No mailbox matching)"
                        ),
                    }
                ],
            }

        with (
            patch(
                "apple_mail_mcp.server.execute_with_core_async",
                side_effect=jxa,
            ),
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch("apple_mail_mcp.server._get_account_map", return_value=amap),
        ):
            from apple_mail_mcp.server import set_flag

            result = await set_flag(5, color="red")

        assert result["failed"] == [5]
        assert result["not_found"] == []
        assert "Message-ID" in result["hint"]  # the name-free route


class TestReadPathFindsUnindexedMessages:
    """The read path demanded an index row; the write path stopped
    needing one long ago.

    Live: get_emails handed out five message_ids and get_email then
    failed on every one with "not found in the index" — the messages
    had arrived after the last sync. The tool that produced the handle
    and the tool that consumes it disagreed about what a handle means.
    """

    @pytest.mark.asyncio
    async def test_falls_back_to_a_live_lookup(self):
        header = "<fresh@x.com>"
        mgr = MagicMock()
        mgr.has_index.return_value = True
        mgr.count_email_locations.return_value = 1
        mgr.is_building.return_value = False
        mgr.write_lock_held.return_value = False
        mgr.last_error = None
        mgr.has_usable_index.return_value = True
        mgr.find_by_rfc822.return_value = []  # not indexed yet
        amap = _mock_acct_map()
        amap.get_cached_accounts.return_value = [{"name": "byte5"}]

        async def jxa(script, **kw):
            assert "normHeaderValue" in script
            return {
                "hit": {
                    "account": "byte5",
                    "mailbox": "Posteingang",
                    "id": 77,
                },
                "capped": 0,
                "unreadable": 0,
            }

        async def by_id(rowid, account=None, mailbox=None):
            return {"id": rowid, "message_id": header, "subject": "fresh"}

        with (
            patch(
                "apple_mail_mcp.server.execute_with_core_async",
                side_effect=jxa,
            ),
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch("apple_mail_mcp.server._get_account_map", return_value=amap),
            patch("apple_mail_mcp.server._get_email_by_id", side_effect=by_id),
        ):
            from apple_mail_mcp.server import get_email

            result = await get_email(header)

        assert result["id"] == 77
        assert result["subject"] == "fresh"

    @pytest.mark.asyncio
    async def test_a_truly_missing_message_says_so_plainly(self):
        mgr = MagicMock()
        mgr.has_index.return_value = True
        mgr.count_email_locations.return_value = 1
        mgr.is_building.return_value = False
        mgr.write_lock_held.return_value = False
        mgr.last_error = None
        mgr.has_usable_index.return_value = True
        mgr.find_by_rfc822.return_value = []
        amap = _mock_acct_map()
        amap.get_cached_accounts.return_value = [{"name": "byte5"}]

        async def nothing(script, **kw):
            return {"hit": None, "capped": 0, "unreadable": 0}

        with (
            patch(
                "apple_mail_mcp.server.execute_with_core_async",
                side_effect=nothing,
            ),
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch("apple_mail_mcp.server._get_account_map", return_value=amap),
        ):
            from apple_mail_mcp.server import get_email

            with pytest.raises(ValueError, match="not in any visible account"):
                await get_email("<gone@x.com>")


class TestFlagColourOnTheDiskPath:
    """The colour is not in the .emlx footer, and guessing at bit
    positions would invent flags. Ask Apple for that one property."""

    @pytest.mark.asyncio
    async def test_unflagged_message_costs_no_extra_call(self):
        from apple_mail_mcp.server import _overlay_flag_color

        calls = []

        async def spy(script, **kw):
            calls.append(script)
            return {"flag_color": "red"}

        with patch(
            "apple_mail_mcp.server.execute_with_core_async", side_effect=spy
        ):
            result = {"flagged": False}
            await _overlay_flag_color(result, 1, "byte5", "Posteingang")

        assert calls == []
        assert "flag_color" not in result

    @pytest.mark.asyncio
    async def test_flagged_message_gets_its_colour(self):
        from apple_mail_mcp.server import _overlay_flag_color

        async def jxa(script, **kw):
            return {"flag_color": "blue"}

        with patch(
            "apple_mail_mcp.server.execute_with_core_async", side_effect=jxa
        ):
            result = {"flagged": True}
            await _overlay_flag_color(result, 1, "byte5", "Posteingang")

        assert result["flag_color"] == "blue"


class TestFlagColoursComeInOneCall:
    """57 flagged messages must not mean 57 process spawns.

    A read-only survey of a flagged mailbox took a minute because each
    message needed its own osascript call just to learn its colour.
    Apple hands out a property for the whole mailbox in one bulk fetch,
    so the cost is fixed per mailbox rather than per message.
    """

    @pytest.mark.asyncio
    async def test_one_call_serves_the_whole_page(self):
        from apple_mail_mcp.server import _overlay_flag_colors_bulk

        rows = [
            {"id": 1, "flagged": True},
            {"id": 2, "flagged": True},
            {"id": 3, "flagged": False},
            {"id": 4, "flagged": True},
        ]
        calls = []

        async def jxa(script, **kw):
            calls.append(script)
            return {"1": "orange", "2": "red", "4": "blue"}

        with patch(
            "apple_mail_mcp.server.execute_with_core_async", side_effect=jxa
        ):
            await _overlay_flag_colors_bulk(rows, "byte5", "Posteingang")

        assert len(calls) == 1  # not one per message
        assert [r.get("flag_color") for r in rows] == [
            "orange",
            "red",
            None,
            "blue",
        ]
        # Only flagged ids are asked for.
        assert "[1, 2, 4]" in calls[0]

    @pytest.mark.asyncio
    async def test_no_flagged_message_means_no_call_at_all(self):
        from apple_mail_mcp.server import _overlay_flag_colors_bulk

        calls = []

        async def jxa(script, **kw):
            calls.append(script)
            return {}

        with patch(
            "apple_mail_mcp.server.execute_with_core_async", side_effect=jxa
        ):
            await _overlay_flag_colors_bulk(
                [{"id": 1, "flagged": False}], "byte5", "Posteingang"
            )

        assert calls == []

    @pytest.mark.asyncio
    async def test_a_failing_lookup_leaves_the_listing_intact(self):
        from apple_mail_mcp.server import _overlay_flag_colors_bulk

        async def boom(script, **kw):
            raise TimeoutError("mailbox too slow")

        rows = [{"id": 1, "flagged": True, "subject": "keep me"}]
        with patch(
            "apple_mail_mcp.server.execute_with_core_async", side_effect=boom
        ):
            await _overlay_flag_colors_bulk(rows, "byte5", "Posteingang")

        assert rows[0]["subject"] == "keep me"
        assert "flag_color" not in rows[0]


class TestAnIncompleteSearchIsNotAVerdict:
    """Found by an external review of today's changes.

    A capped scan, an unreadable mailbox, a timeout or a refused
    Automation permission all leave the question open. Answering them
    with "most likely deleted" turns an incomplete search into a
    statement about the user's mail — the same defect that, in its
    write-path form, cost a full day of debugging.
    """

    def _ctx(self, jxa):
        mgr = MagicMock()
        mgr.has_index.return_value = True
        mgr.count_email_locations.return_value = 1
        mgr.is_building.return_value = False
        mgr.write_lock_held.return_value = False
        mgr.last_error = None
        mgr.has_usable_index.return_value = True
        mgr.find_by_rfc822.return_value = []
        amap = _mock_acct_map()
        amap.get_cached_accounts.return_value = [{"name": "byte5"}]
        return (
            patch(
                "apple_mail_mcp.server.execute_with_core_async",
                side_effect=jxa,
            ),
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch("apple_mail_mcp.server._get_account_map", return_value=amap),
        )

    @pytest.mark.asyncio
    async def test_capped_scan_says_the_search_was_incomplete(self):
        async def capped(script, **kw):
            return {"hit": None, "capped": 12, "unreadable": 0}

        a, b, c = self._ctx(capped)
        with a, b, c:
            from apple_mail_mcp.server import get_email

            with pytest.raises(ValueError) as err:
                await get_email("<x@y.com>")

        text = str(err.value)
        assert "could not be completed" in text
        assert "12 mailbox" in text
        assert "deleted" not in text  # never a verdict

    @pytest.mark.asyncio
    async def test_unreadable_mailbox_is_not_a_missing_message(self):
        async def unreadable(script, **kw):
            return {"hit": None, "capped": 0, "unreadable": 3}

        a, b, c = self._ctx(unreadable)
        with a, b, c:
            from apple_mail_mcp.server import get_email

            with pytest.raises(ValueError, match="could not be completed"):
                await get_email("<x@y.com>")

    @pytest.mark.asyncio
    async def test_a_refused_apple_event_is_not_a_missing_message(self):
        async def refused(script, **kw):
            raise RuntimeError("Not authorized to send Apple events (-1743)")

        a, b, c = self._ctx(refused)
        with a, b, c:
            from apple_mail_mcp.server import get_email

            with pytest.raises(ValueError) as err:
                await get_email("<x@y.com>")

        assert "-1743" in str(err.value)
        assert "says nothing about whether the message exists" in str(err.value)

    @pytest.mark.asyncio
    async def test_a_complete_search_may_still_conclude_absence(self):
        """The distinction only helps if a real miss stays a real miss."""

        async def clean_miss(script, **kw):
            return {"hit": None, "capped": 0, "unreadable": 0}

        a, b, c = self._ctx(clean_miss)
        with a, b, c:
            from apple_mail_mcp.server import get_email

            with pytest.raises(ValueError) as err:
                await get_email("<x@y.com>")

        assert "every mailbox was searched" in str(err.value)


class TestRecoveryAndStrategy3KeepTheirGaps:
    """Third review round, same property, two more places.

    Stable-id recovery discarded its own scan counters and its failures,
    and get_email's all-mailbox scan threw a bare "not found" whether it
    had searched everything or given up at the cap. Both let an
    unfinished search read as proof the message is gone.
    """

    def test_strategy3_scan_reports_what_it_skipped(self):
        from apple_mail_mcp.builders import GetEmailBuilder

        js = GetEmailBuilder(message_id=42, account="byte5").build()
        assert "let unsearched = Math.max(0, allMailboxes.length" in js
        assert "unsearched++" in js
        assert "INCOMPLETE:" in js

    @pytest.mark.asyncio
    async def test_incomplete_strategy3_does_not_claim_absence(self):
        mgr = MagicMock()
        mgr.has_index.return_value = False
        mgr.is_building.return_value = False
        mgr.write_lock_held.return_value = False
        mgr.last_error = None
        mgr.has_usable_index.return_value = False
        mgr.has_usable_index.return_value = False

        async def incomplete(script, **kw):
            raise RuntimeError(
                "Error: Message not found with ID: 42 "
                "(INCOMPLETE: 9 mailbox(es) not searched)"
            )

        with (
            patch(
                "apple_mail_mcp.server.execute_with_core_async",
                side_effect=incomplete,
            ),
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch(
                "apple_mail_mcp.server._resolve_visible_account",
                AsyncMock(return_value="byte5"),
            ),
        ):
            from apple_mail_mcp.server import get_email

            with pytest.raises(ValueError) as err:
                await get_email(42)

        text = str(err.value)
        assert "search was incomplete" in text
        assert "9 mailbox" in text
        assert "does not mean the message is gone" in text

    @pytest.mark.asyncio
    async def test_a_failed_recovery_counts_as_unsearched(self):
        """Recovery that never ran must not harden a miss into a fact."""

        def locate(mid, account=None, mailbox=None):
            return ("uuid-work", "INBOX")

        mgr = MagicMock()
        mgr.has_index.return_value = True
        mgr.count_email_locations.return_value = 1
        mgr.is_building.return_value = False
        mgr.write_lock_held.return_value = False
        mgr.last_error = None
        mgr.has_usable_index.return_value = True
        mgr.find_email_location.side_effect = locate
        mgr.get_rfc822_id.return_value = "<moved@x>"
        amap = _mock_acct_map()

        async def router(script, **kw):
            if '"by_header": true' in script:
                raise TimeoutError("recovery wedged")
            return {"updated": [], "unchanged": [], "not_found": [5]}

        with (
            patch(
                "apple_mail_mcp.server.execute_with_core_async",
                side_effect=router,
            ),
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch("apple_mail_mcp.server._get_account_map", return_value=amap),
        ):
            from apple_mail_mcp.server import set_read_status

            result = await set_read_status(5)

        assert result["not_found"] == [5]
        assert result["diagnostics"]["mailboxes_not_searched"] >= 1


class TestAStalePathIsNotADeletedMessage:
    """Fifth review round, the last one.

    A missing .emlx file means the path recorded in the index is wrong.
    It does not mean the message left Apple Mail — it may have been
    re-filed, or Mail may have rebuilt its store. The old code raised
    "deleted or moved" right there and skipped the live strategies,
    justified by the assumption that they would fail anyway. That
    assumption was never verified.
    """

    def test_the_shortcut_is_gone(self):
        import inspect

        from apple_mail_mcp import server

        src = inspect.getsource(server)
        assert "deleted or moved since the last" not in src
        assert "KEEP GOING" in src


class TestNumericIdCannotSpeakForOtherAccounts:
    """Sixth round. Strategy 3 walks ONE account.

    With six accounts configured, "message not found" after searching
    one of them is an absence claim about five that nobody looked in.
    A numeric id is only unique within a mailbox, so it cannot be
    searched across accounts at all — which makes saying so, and
    pointing at the Message-ID, the only honest answer.
    """

    @pytest.mark.asyncio
    async def test_says_which_accounts_were_not_searched(self):
        mgr = MagicMock()
        mgr.has_index.return_value = False
        mgr.is_building.return_value = False
        mgr.write_lock_held.return_value = False
        mgr.last_error = None
        mgr.has_usable_index.return_value = False
        mgr.has_usable_index.return_value = False

        async def gone(script, **kw):
            raise RuntimeError("Error: Message not found with ID: 42")

        with (
            patch(
                "apple_mail_mcp.server.execute_with_core_async",
                side_effect=gone,
            ),
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch(
                "apple_mail_mcp.server._resolve_visible_account",
                AsyncMock(return_value="iCloud"),
            ),
            patch(
                "apple_mail_mcp.server._visible_account_names",
                AsyncMock(return_value=["iCloud", "byte5", "freenea"]),
            ),
        ):
            from apple_mail_mcp.server import get_email

            with pytest.raises(ValueError) as err:
                await get_email(42)

        text = str(err.value)
        assert "2 account(s) were not searched" in text
        assert "Message-ID" in text  # the handle that does span accounts

    @pytest.mark.asyncio
    async def test_a_single_account_setup_still_says_not_found(self):
        """With one account, one account IS everywhere."""
        mgr = MagicMock()
        mgr.has_index.return_value = False
        mgr.is_building.return_value = False
        mgr.write_lock_held.return_value = False
        mgr.last_error = None
        mgr.has_usable_index.return_value = False
        mgr.has_usable_index.return_value = False

        async def gone(script, **kw):
            raise RuntimeError("Error: Message not found with ID: 42")

        with (
            patch(
                "apple_mail_mcp.server.execute_with_core_async",
                side_effect=gone,
            ),
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch(
                "apple_mail_mcp.server._resolve_visible_account",
                AsyncMock(return_value="byte5"),
            ),
            patch(
                "apple_mail_mcp.server._visible_account_names",
                AsyncMock(return_value=["byte5"]),
            ),
        ):
            from apple_mail_mcp.server import get_email

            with pytest.raises(ValueError) as err:
                await get_email(42)

        assert "not searched" not in str(err.value)


class TestBatchReadsAndCrossAccountListing:
    """Fewer round-trips for surveys and triage.

    Measured motivation: a colour survey of 57 flagged messages needed
    58 tool calls. The reads themselves are 1-5ms of disk; the cost was
    entirely in the round-trips.
    """

    @pytest.mark.asyncio
    async def test_a_list_returns_one_entry_per_reference(self):
        async def by_header(h, account=None, mailbox=None):
            return {"message_id": h, "subject": f"re {h}"}

        with patch(
            "apple_mail_mcp.server._get_email_by_header", side_effect=by_header
        ):
            from apple_mail_mcp.server import get_email

            out = await get_email(["<a@x>", "<b@x>"])

        assert [e["ref"] for e in out] == ["<a@x>", "<b@x>"]
        assert out[0]["email"]["subject"] == "re <a@x>"

    @pytest.mark.asyncio
    async def test_one_bad_reference_does_not_sink_the_batch(self):
        async def flaky(h, account=None, mailbox=None):
            if h == "<bad@x>":
                raise ValueError("not found")
            return {"message_id": h}

        with patch(
            "apple_mail_mcp.server._get_email_by_header", side_effect=flaky
        ):
            from apple_mail_mcp.server import get_email

            out = await get_email(["<a@x>", "<bad@x>", "<c@x>"])

        assert "email" in out[0]
        assert out[1]["error"] == "not found"
        assert "email" in out[2]

    @pytest.mark.asyncio
    async def test_a_single_reference_keeps_the_old_shape(self):
        """Existing callers must not have to change."""

        async def by_id(mid, account=None, mailbox=None):
            return {"id": mid, "subject": "single"}

        with patch("apple_mail_mcp.server._get_email_by_id", side_effect=by_id):
            from apple_mail_mcp.server import get_email

            out = await get_email(42)

        assert out["subject"] == "single"  # not a list

    @pytest.mark.asyncio
    async def test_oversized_batch_is_refused_with_the_reason(self):
        from apple_mail_mcp.server import MAX_READ_BATCH, get_email

        refs = [f"<m{i}@x>" for i in range(MAX_READ_BATCH + 1)]
        with pytest.raises(ValueError, match="context"):
            await get_email(refs)

    @pytest.mark.asyncio
    async def test_account_all_drops_both_defaults(self):
        """ "all" must not be narrowed by the INBOX default either."""
        captured = {}

        def fetch(
            env_path, *, account_uuid, mailbox_name, filter_kind, limit, **kw
        ):
            captured["account_uuid"] = account_uuid
            captured["mailbox_name"] = mailbox_name
            return []

        mgr = MagicMock()
        amap = _mock_acct_map()
        amap.get_cached_accounts.return_value = [
            {"name": "byte5", "id": "uuid-byte5"}
        ]

        with (
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch("apple_mail_mcp.server._get_account_map", return_value=amap),
            patch(
                "apple_mail_mcp.index.envelope_direct.fetch_recent_messages",
                side_effect=fetch,
            ),
            patch(
                "apple_mail_mcp.index.envelope_direct.envelope_index_path",
                return_value=MagicMock(exists=lambda: True),
            ),
            patch(
                "apple_mail_mcp.index.disk.find_mail_directory",
                return_value=Path("/tmp/mail"),
            ),
        ):
            from apple_mail_mcp.server import get_emails

            await get_emails(account="all")

        assert captured["account_uuid"] is None  # every account
        assert captured["mailbox_name"] is None  # every mailbox


class TestExcludedAccountsSurviveTheAllShortcut:
    """ "all" must not become a hole in the exclusion boundary (#90)."""

    @pytest.mark.asyncio
    async def test_hidden_account_rows_are_dropped(self):
        from types import SimpleNamespace

        rows = [
            SimpleNamespace(
                message_id=1,
                subject="visible",
                sender="a@x",
                date_received="2026-07-28T10:00:00",
                read=False,
                flagged=False,
                account_uuid="uuid-visible",
                mailbox_name="Posteingang",
            ),
            SimpleNamespace(
                message_id=2,
                subject="secret",
                sender="b@x",
                date_received="2026-07-28T10:00:00",
                read=False,
                flagged=False,
                account_uuid="uuid-secret",
                mailbox_name="Posteingang",
            ),
        ]

        mgr = MagicMock()
        mgr.has_index.return_value = False
        mgr.is_building.return_value = False
        mgr.write_lock_held.return_value = False
        mgr.last_error = None
        mgr.has_usable_index.return_value = False
        amap = _mock_acct_map(excluded_uuids={"uuid-secret"})
        amap.get_cached_accounts.return_value = [
            {"name": "byte5", "id": "uuid-visible"},
            {"name": "Secret", "id": "uuid-secret"},
        ]

        with (
            patch(
                "apple_mail_mcp.server._excluded_account_names",
                return_value={"Secret"},
            ),
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch("apple_mail_mcp.server._get_account_map", return_value=amap),
            patch(
                "apple_mail_mcp.index.envelope_direct.fetch_recent_messages",
                return_value=rows,
            ),
            patch(
                "apple_mail_mcp.index.envelope_direct.envelope_index_path",
                return_value=MagicMock(exists=lambda: True),
            ),
            patch(
                "apple_mail_mcp.index.disk.find_mail_directory",
                return_value=Path("/tmp/mail"),
            ),
        ):
            from apple_mail_mcp.server import get_emails

            out = await get_emails(account="all")

        assert [e["subject"] for e in out] == ["visible"]


class TestBacklogCanBeWalkedBackwards:
    """Reported by a scheduled triage run: the backlog was unreachable.

    `get_emails` only ever returned the newest N per mailbox, so a
    stored cursor deep in the mailbox could not be approached, and
    `search()` requires keywords and is therefore useless for a gapless
    reverse scan. That was a genuine tool limit, not an omission by the
    agent.
    """

    def _capture(self, **kwargs):
        seen = {}

        def fetch(env_path, **kw):
            seen.update(kw)
            return []

        return seen, fetch

    @pytest.mark.asyncio
    async def test_before_and_offset_reach_the_sql_layer(self):
        seen, fetch = self._capture()
        mgr = MagicMock()
        amap = _mock_acct_map()
        amap.get_cached_accounts.return_value = [
            {"name": "byte5", "id": "uuid-byte5"}
        ]
        with (
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch("apple_mail_mcp.server._get_account_map", return_value=amap),
            patch(
                "apple_mail_mcp.index.envelope_direct.fetch_recent_messages",
                side_effect=fetch,
            ),
            patch(
                "apple_mail_mcp.index.envelope_direct.envelope_index_path",
                return_value=MagicMock(exists=lambda: True),
            ),
            patch(
                "apple_mail_mcp.index.disk.find_mail_directory",
                return_value=Path("/tmp/mail"),
            ),
        ):
            from apple_mail_mcp.server import get_emails

            await get_emails(
                account="all", before="2026-01-15T10:00:00", offset=20
            )

        assert seen["before"] is not None
        assert seen["offset"] == 20
        assert seen["after"] is None

    def test_a_bad_date_says_what_is_expected(self):
        from apple_mail_mcp.server import _parse_date_bound

        assert _parse_date_bound(None, "before") is None
        assert _parse_date_bound("2026-07-28", "before") > 0
        with pytest.raises(ValueError, match="ISO date"):
            _parse_date_bound("last tuesday", "before")

    @pytest.mark.asyncio
    async def test_the_jxa_fallback_refuses_rather_than_ignoring(self):
        """Dropping the window would page the same newest N forever."""
        mgr = MagicMock()
        mgr.has_index.return_value = False
        mgr.is_building.return_value = False
        mgr.write_lock_held.return_value = False
        mgr.last_error = None
        mgr.has_usable_index.return_value = False
        with (
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch(
                "apple_mail_mcp.server._resolve_visible_account",
                AsyncMock(return_value="byte5"),
            ),
            patch(
                "apple_mail_mcp.index.disk.find_mail_directory",
                side_effect=FileNotFoundError("no mail dir"),
            ),
        ):
            from apple_mail_mcp.server import get_emails

            with pytest.raises(ValueError, match="Envelope Index"):
                await get_emails(before="2026-01-15")


class TestAnIncompleteSearchIsNotAnAbsence:
    """A message may be called missing only when the search covered
    everywhere it could be.

    A mailbox cap, a mailbox Mail refuses to read, an account that
    cannot be enumerated — all of them produce the identical empty
    answer, which is what makes the defect expensive: three different
    causes are indistinguishable from outside.
    """

    def test_the_scan_counts_what_it_skipped(self):
        from apple_mail_mcp.builders import GetEmailBuilder

        js = GetEmailBuilder(message_id=42, account="Work").build()
        assert "let unsearched = Math.max(0, allMailboxes.length" in js
        assert "unsearched++" in js
        assert "INCOMPLETE:" in js

    def test_an_unreadable_account_is_marked_incomplete(self):
        from apple_mail_mcp.builders import GetEmailBuilder

        js = GetEmailBuilder(message_id=42, account="Work").build()
        assert "the account could not be read at all" in js
        # The throw must sit before any per-mailbox loop.
        assert js.index("could not be read at all") < js.index("mbLimit =")

    @pytest.mark.asyncio
    async def test_incomplete_scan_does_not_claim_absence(self):
        from unittest.mock import MagicMock, patch

        mgr = MagicMock()
        mgr.has_index.return_value = False
        mgr.is_building.return_value = False
        mgr.write_lock_held.return_value = False
        mgr.last_error = None
        mgr.has_usable_index.return_value = False

        async def incomplete(script, **kw):
            raise RuntimeError(
                "Error: Message not found with ID: 42 "
                "(INCOMPLETE: 9 mailbox(es) not searched)"
            )

        with (
            patch(
                "apple_mail_mcp.server.execute_with_core_async",
                side_effect=incomplete,
            ),
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
        ):
            from apple_mail_mcp.server import get_email

            with pytest.raises(ValueError) as err:
                await get_email(42)

        text = str(err.value)
        assert "search was incomplete" in text
        assert "9 mailbox" in text
        assert "does not mean the message is gone" in text

    @pytest.mark.asyncio
    async def test_a_complete_scan_may_still_report_not_found(self):
        """The distinction only helps if a real miss stays a real miss."""
        from unittest.mock import MagicMock, patch

        mgr = MagicMock()
        mgr.has_index.return_value = False
        mgr.is_building.return_value = False
        mgr.write_lock_held.return_value = False
        mgr.last_error = None
        mgr.has_usable_index.return_value = False

        async def clean_miss(script, **kw):
            raise RuntimeError("Error: Message not found with ID: 42")

        with (
            patch(
                "apple_mail_mcp.server.execute_with_core_async",
                side_effect=clean_miss,
            ),
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
        ):
            from apple_mail_mcp.server import get_email

            with pytest.raises(ValueError, match="not found"):
                await get_email(42)


class TestAnUnconfirmedStartIsNotAStart:
    """A build that never signals it began is not a running build. The
    five-second wait timing out used to return "started" all the same,
    so a stuck rebuild looked exactly like a healthy one."""

    @pytest.mark.asyncio
    async def test_a_build_that_never_signals_is_not_started(self):
        import threading
        from unittest.mock import MagicMock, patch

        import apple_mail_mcp.server as srv

        release = threading.Event()
        mgr = MagicMock()
        mgr.has_index.return_value = True
        mgr.count_email_locations.return_value = 1
        mgr.is_building.return_value = False
        mgr.write_lock_held.return_value = False
        mgr.last_error = None
        mgr.has_usable_index.return_value = True
        # Alive, but never reaches on_started() — the shape of a build
        # wedged in Mail's Apple Events permission dialog.
        mgr.build_from_disk.side_effect = lambda **kw: release.wait(10)

        try:
            with (
                patch.object(srv, "_get_index_manager", return_value=mgr),
                patch.object(srv, "BUILD_START_TIMEOUT", 0.05),
            ):
                r = await srv.refresh_index(full=True)
        finally:
            release.set()

        assert r["status"] == "unconfirmed"
        assert r["status"] != "started"


class TestASyncThatCouldNotReadMailIsNotCompleted:
    """`sync_updates()` returns 0 both when nothing changed and when it
    could not reach ~/Library/Mail at all. Reporting the second as
    "already up to date" tells a user without Full Disk Access that mail
    nobody read has been indexed. Here the manager records the reason in
    `last_error` and this tool has to consult it."""

    @pytest.mark.asyncio
    async def test_no_full_disk_access_is_reported_as_failure(self):
        from unittest.mock import MagicMock, patch

        import apple_mail_mcp.server as srv

        mgr = MagicMock()
        mgr.has_index.return_value = True
        mgr.has_usable_index.return_value = True
        mgr.is_building.return_value = False
        mgr.write_lock_held.return_value = False
        mgr.count_email_locations.return_value = 1

        def _sync(*a, **kw):
            mgr.last_error = "PermissionError: no Full Disk Access"
            return 0

        mgr.last_error = None
        mgr.sync_updates.side_effect = _sync

        with patch.object(srv, "_get_index_manager", return_value=mgr):
            r = await srv.refresh_index()

        assert r["status"] != "completed", (
            "a sync that never read Mail was reported as up to date"
        )
        assert "full disk access" in str(r).lower()


class TestBuildFailuresReachTheLogFile:
    """Installing a handler is not logging.

    `index` set up file logging and then reported its failure with
    `print(..., file=sys.stderr)` only — so the command most likely to
    fail left `server.log` empty, which is precisely the situation the
    file exists for.
    """

    def test_the_index_command_logs_its_failure(self, tmp_path, monkeypatch):
        import logging
        from unittest.mock import patch

        target = tmp_path / "server.log"
        monkeypatch.setenv("APPLE_MAIL_LOG_PATH", str(target))

        from apple_mail_mcp import cli

        root = logging.getLogger("apple_mail_mcp")
        try:
            with (
                patch.object(
                    cli,
                    "_run_optionally_profiled",
                    side_effect=PermissionError("no Full Disk Access"),
                ),
                pytest.raises(SystemExit),
            ):
                cli.index()
            for h in root.handlers:
                h.flush()
            assert target.exists()
            assert "no Full Disk Access" in target.read_text()
        finally:
            for h in list(root.handlers):
                h.close()
                root.removeHandler(h)

    def test_a_failed_startup_sync_logs_its_failure(
        self, tmp_path, monkeypatch
    ):
        """The failure most likely to go unnoticed. `serve` runs under a
        desktop client, where stderr goes nowhere the user will look —
        so a corrupt or unreadable index stopped the sync and left the
        log file empty, which reads as a healthy server."""
        import logging
        import threading
        from unittest.mock import MagicMock, patch

        target = tmp_path / "server.log"
        monkeypatch.setenv("APPLE_MAIL_LOG_PATH", str(target))

        from apple_mail_mcp import cli

        mgr = MagicMock()
        mgr.has_index.return_value = True
        mgr.count_email_locations.return_value = 1
        mgr.is_building.return_value = False
        mgr.write_lock_held.return_value = False
        mgr.last_error = None
        mgr.has_usable_index.return_value = True
        mgr.sync_updates.side_effect = RuntimeError("index is corrupt")

        class _RunInline(threading.Thread):
            def start(self) -> None:  # run the sync body synchronously
                self.run()

        root = logging.getLogger("apple_mail_mcp")
        try:
            with (
                patch(
                    "apple_mail_mcp.index.IndexManager.get_instance",
                    return_value=mgr,
                ),
                patch("threading.Thread", _RunInline),
                patch("apple_mail_mcp.server.mcp.run"),
            ):
                cli._run_serve(watch=False)
            for h in root.handlers:
                h.flush()
            assert target.exists()
            assert "index is corrupt" in target.read_text(), (
                "the startup sync failed and server.log never said so"
            )
        finally:
            for h in list(root.handlers):
                h.close()
                root.removeHandler(h)


class TestCrossAccountListing:
    """`get_emails(account="all")` — one call for every account.

    The Envelope Index query already means "every account" when given no
    UUID; only this tool's defaulting stood in the way. Without it, an
    inbox review costs one call per account and the caller has to know
    the account names first.
    """

    def _env_patches(self, amap, fetch):
        mgr = MagicMock()
        mgr.has_index.return_value = False
        mgr.is_building.return_value = False
        mgr.write_lock_held.return_value = False
        mgr.last_error = None
        mgr.has_usable_index.return_value = False
        return (
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch("apple_mail_mcp.server._get_account_map", return_value=amap),
            patch(
                "apple_mail_mcp.index.envelope_direct.fetch_recent_messages",
                **fetch,
            ),
            patch(
                "apple_mail_mcp.index.envelope_direct.envelope_index_path",
                return_value=MagicMock(exists=lambda: True),
            ),
            patch(
                "apple_mail_mcp.index.disk.find_mail_directory",
                return_value=Path("/tmp/mail"),
            ),
        )

    @pytest.mark.asyncio
    async def test_all_drops_both_defaults(self):
        """ "all" must not be narrowed by the INBOX default either: it
        would keep whichever accounts happen to have a mailbox by that
        name — on a localized Mail, none of them."""
        captured = {}

        def fetch(env_path, *, account_uuid, mailbox_name, **kw):
            captured["account_uuid"] = account_uuid
            captured["mailbox_name"] = mailbox_name
            return []

        amap = _acct_map()
        amap.get_cached_accounts.return_value = [
            {"name": "Work", "id": "uuid-work"}
        ]
        with (
            patch("apple_mail_mcp.server._get_account_map", return_value=amap),
            patch(
                "apple_mail_mcp.index.envelope_direct.fetch_recent_messages",
                side_effect=fetch,
            ),
            patch(
                "apple_mail_mcp.index.envelope_direct.envelope_index_path",
                return_value=MagicMock(exists=lambda: True),
            ),
            patch(
                "apple_mail_mcp.index.disk.find_mail_directory",
                return_value=Path("/tmp/mail"),
            ),
        ):
            from apple_mail_mcp.server import get_emails

            await get_emails(account="all")

        assert captured["account_uuid"] is None  # every account
        assert captured["mailbox_name"] is None  # every mailbox

    @pytest.mark.asyncio
    async def test_an_explicit_mailbox_still_applies_under_all(self):
        captured = {}

        def fetch(env_path, *, account_uuid, mailbox_name, **kw):
            captured["mailbox_name"] = mailbox_name
            return []

        amap = _acct_map()
        amap.get_cached_accounts.return_value = []
        with (
            patch("apple_mail_mcp.server._get_account_map", return_value=amap),
            patch(
                "apple_mail_mcp.index.envelope_direct.fetch_recent_messages",
                side_effect=fetch,
            ),
            patch(
                "apple_mail_mcp.index.envelope_direct.envelope_index_path",
                return_value=MagicMock(exists=lambda: True),
            ),
            patch(
                "apple_mail_mcp.index.disk.find_mail_directory",
                return_value=Path("/tmp/mail"),
            ),
        ):
            from apple_mail_mcp.server import get_emails

            await get_emails(account="all", mailbox="Sent")

        assert captured["mailbox_name"] == "Sent"

    @pytest.mark.asyncio
    async def test_every_row_names_its_account(self):
        """A flat list across six accounts is unusable without it — the
        caller cannot tell whose inbox a message came from."""
        from types import SimpleNamespace

        rows = [
            SimpleNamespace(
                message_id=i,
                subject=f"s{i}",
                sender="a@x",
                date_received="2026-07-28T10:00:00",
                read=False,
                flagged=False,
                account_uuid=uuid,
                mailbox_name="INBOX",
            )
            for i, uuid in ((1, "uuid-work"), (2, "uuid-private"))
        ]
        amap = _acct_map()
        amap.uuid_to_name.side_effect = lambda u: {
            "uuid-work": "Work",
            "uuid-private": "Private",
        }.get(u)
        amap.get_cached_accounts.return_value = []
        with (
            patch("apple_mail_mcp.server._get_account_map", return_value=amap),
            patch(
                "apple_mail_mcp.index.envelope_direct.fetch_recent_messages",
                return_value=rows,
            ),
            patch(
                "apple_mail_mcp.index.envelope_direct.envelope_index_path",
                return_value=MagicMock(exists=lambda: True),
            ),
            patch(
                "apple_mail_mcp.index.disk.find_mail_directory",
                return_value=Path("/tmp/mail"),
            ),
        ):
            from apple_mail_mcp.server import get_emails

            out = await get_emails(account="all")

        assert [e["account"] for e in out] == ["Work", "Private"]

    @pytest.mark.asyncio
    async def test_a_single_account_listing_keeps_its_old_shape(self):
        """Adding the field everywhere would change every existing
        response for no gain."""
        from types import SimpleNamespace

        rows = [
            SimpleNamespace(
                message_id=1,
                subject="s",
                sender="a@x",
                date_received="2026-07-28T10:00:00",
                read=False,
                flagged=False,
                account_uuid="uuid-work",
                mailbox_name="INBOX",
            )
        ]
        amap = _acct_map()
        amap.name_to_uuid.return_value = "uuid-work"
        amap.get_cached_accounts.return_value = [
            {"name": "Work", "id": "uuid-work"}
        ]
        with (
            patch("apple_mail_mcp.server._get_account_map", return_value=amap),
            patch(
                "apple_mail_mcp.index.envelope_direct.fetch_recent_messages",
                return_value=rows,
            ),
            patch(
                "apple_mail_mcp.index.envelope_direct.envelope_index_path",
                return_value=MagicMock(exists=lambda: True),
            ),
            patch(
                "apple_mail_mcp.index.disk.find_mail_directory",
                return_value=Path("/tmp/mail"),
            ),
        ):
            from apple_mail_mcp.server import get_emails

            out = await get_emails(account="Work")

        assert "account" not in out[0]

    @pytest.mark.asyncio
    async def test_excluded_accounts_survive_the_shortcut(self):
        """ "all" must not become a hole in the exclusion boundary."""
        from types import SimpleNamespace

        def row(mid, subject, uuid):
            return SimpleNamespace(
                message_id=mid,
                subject=subject,
                sender="a@x",
                date_received="2026-07-28T10:00:00",
                read=False,
                flagged=False,
                account_uuid=uuid,
                mailbox_name="INBOX",
            )

        amap = _acct_map(excluded_uuids={"uuid-secret"})
        amap.get_cached_accounts.return_value = [
            {"name": "Work", "id": "uuid-visible"},
            {"name": "Secret", "id": "uuid-secret"},
        ]
        with (
            patch(
                "apple_mail_mcp.server._excluded_account_names",
                return_value={"Secret"},
            ),
            patch("apple_mail_mcp.server._get_account_map", return_value=amap),
            patch(
                "apple_mail_mcp.index.envelope_direct.fetch_recent_messages",
                return_value=[
                    row(1, "visible", "uuid-visible"),
                    row(2, "secret", "uuid-secret"),
                ],
            ),
            patch(
                "apple_mail_mcp.index.envelope_direct.envelope_index_path",
                return_value=MagicMock(exists=lambda: True),
            ),
            patch(
                "apple_mail_mcp.index.disk.find_mail_directory",
                return_value=Path("/tmp/mail"),
            ),
        ):
            from apple_mail_mcp.server import get_emails

            out = await get_emails(account="all")

        assert [e["subject"] for e in out] == ["visible"]

    @pytest.mark.asyncio
    async def test_jxa_fallback_refuses_rather_than_answering_narrowly(self):
        """JXA walks one account at a time. Falling through would answer
        a different question than the one that was asked."""
        with (
            patch(
                "apple_mail_mcp.index.disk.find_mail_directory",
                side_effect=FileNotFoundError("no ~/Library/Mail"),
            ),
            pytest.raises(ValueError, match="all accounts"),
        ):
            from apple_mail_mcp.server import get_emails

            await get_emails(account="all")


class TestEnvelopeWindowSql:
    """The window and offset in the SQL itself."""

    def _db(self, tmp_path):
        """A minimal Envelope Index with four dated messages."""
        import sqlite3

        path = tmp_path / "Envelope Index"
        conn = sqlite3.connect(path)
        conn.executescript("""
            CREATE TABLE messages (
                ROWID INTEGER PRIMARY KEY, message_id INTEGER,
                subject INTEGER, sender INTEGER, date_received INTEGER,
                mailbox INTEGER, read INTEGER, flagged INTEGER,
                deleted INTEGER DEFAULT 0
            );
            CREATE TABLE subjects (ROWID INTEGER PRIMARY KEY, subject TEXT);
            CREATE TABLE addresses (
                ROWID INTEGER PRIMARY KEY, address TEXT, comment TEXT
            );
            CREATE TABLE mailboxes (ROWID INTEGER PRIMARY KEY, url TEXT);
            INSERT INTO mailboxes VALUES (1, 'imap://uuid-work/INBOX');
            INSERT INTO addresses VALUES (1, 'a@x', '');
        """)
        for i, ts in enumerate([4000, 3000, 2000, 1000], start=1):
            conn.execute("INSERT INTO subjects VALUES (?, ?)", (i, f"msg {ts}"))
            conn.execute(
                "INSERT INTO messages (ROWID, message_id, subject, sender,"
                " date_received, mailbox, read, flagged, deleted)"
                " VALUES (?, ?, ?, 1, ?, 1, 0, 0, 0)",
                (i, i, i, ts),
            )
        conn.commit()
        conn.close()
        return path

    def _fetch(self, path, **kw):
        from apple_mail_mcp.index.envelope_direct import fetch_recent_messages

        kw.setdefault("limit", 10)
        return fetch_recent_messages(
            path,
            account_uuid=None,
            mailbox_name=None,
            filter_kind="all",
            **kw,
        )

    def test_before_is_exclusive_and_ordered_newest_first(self, tmp_path):
        rows = self._fetch(self._db(tmp_path), before=3000)
        assert [r.subject for r in rows] == ["msg 2000", "msg 1000"]

    def test_after_is_exclusive(self, tmp_path):
        rows = self._fetch(self._db(tmp_path), after=3000)
        assert [r.subject for r in rows] == ["msg 4000"]

    def test_offset_skips_from_the_newest(self, tmp_path):
        rows = self._fetch(self._db(tmp_path), offset=2)
        assert [r.subject for r in rows] == ["msg 2000", "msg 1000"]

    def test_equal_timestamps_are_not_skipped_at_a_page_boundary(
        self, tmp_path
    ):
        """Mail stores whole seconds. With a strict `<` on the timestamp
        alone, every row sharing the oldest second of a page becomes
        unreachable forever — the exact defect this unit exists to
        remove, reintroduced at the boundary."""
        import sqlite3

        path = tmp_path / "Envelope Index"
        conn = sqlite3.connect(path)
        conn.executescript("""
            CREATE TABLE messages (
                ROWID INTEGER PRIMARY KEY, message_id INTEGER,
                subject INTEGER, sender INTEGER, date_received INTEGER,
                mailbox INTEGER, read INTEGER, flagged INTEGER,
                deleted INTEGER DEFAULT 0
            );
            CREATE TABLE subjects (ROWID INTEGER PRIMARY KEY, subject TEXT);
            CREATE TABLE addresses (
                ROWID INTEGER PRIMARY KEY, address TEXT, comment TEXT
            );
            CREATE TABLE mailboxes (ROWID INTEGER PRIMARY KEY, url TEXT);
            INSERT INTO mailboxes VALUES (1, 'imap://uuid/INBOX');
            INSERT INTO addresses VALUES (1, 'a@x', '');
        """)
        # Three messages, one and the same second.
        for i in (1, 2, 3):
            conn.execute("INSERT INTO subjects VALUES (?, ?)", (i, f"m{i}"))
            conn.execute(
                "INSERT INTO messages (ROWID, message_id, subject, sender,"
                " date_received, mailbox, read, flagged, deleted)"
                " VALUES (?, ?, ?, 1, 1000, 1, 0, 0, 0)",
                (i, i, i),
            )
        conn.commit()
        conn.close()

        page1 = self._fetch(path, limit=2)
        assert len(page1) == 2

        # The cursor the caller actually has: the oldest row it saw.
        oldest = page1[-1]
        page2 = self._fetch(
            path, limit=2, before=1000, before_id=oldest.message_id
        )
        assert [r.subject for r in page2] == ["m1"], (
            "the third message of that second must still be reachable"
        )

    def test_a_window_and_offset_compose(self, tmp_path):
        rows = self._fetch(self._db(tmp_path), before=4000, offset=1)
        assert [r.subject for r in rows] == ["msg 2000", "msg 1000"]


class TestEveryOutputBoundaryConverts:
    """ "Every output boundary" has to mean every one of them.

    The JXA listing fallback was left in UTC, so the same tool answered
    in two different zones depending on whether Apple's Envelope Index
    happened to be readable — the inconsistency is worse than either
    choice on its own.
    """

    @pytest.mark.asyncio
    async def test_the_jxa_listing_fallback_is_converted(self):
        from unittest.mock import AsyncMock, MagicMock, patch

        rows = [
            {
                "id": 1,
                "subject": "s",
                "sender": "a@x",
                "date_received": "2026-07-28T10:00:00+00:00",
                "read": False,
                "flagged": False,
            }
        ]
        with (
            patch(
                "apple_mail_mcp.index.disk.find_mail_directory",
                side_effect=FileNotFoundError("no envelope index"),
            ),
            patch(
                "apple_mail_mcp.server._resolve_visible_account",
                AsyncMock(return_value="Work"),
            ),
            patch(
                "apple_mail_mcp.server.execute_query_async",
                AsyncMock(return_value=rows),
            ),
            patch(
                "apple_mail_mcp.server._get_index_manager",
                return_value=MagicMock(),
            ),
        ):
            from apple_mail_mcp.server import get_emails, to_local_iso

            out = await get_emails()

        assert out[0]["date_received"] == to_local_iso(
            "2026-07-28T10:00:00+00:00"
        )
        # And that is genuinely a conversion, not a pass-through, unless
        # this machine happens to run in UTC.
        import time

        if time.timezone or time.altzone:
            assert out[0]["date_received"] != "2026-07-28T10:00:00+00:00"


class TestHalfACursorIsRefused:
    """`before_id` without `before` used to be dropped, and the caller
    got the NEWEST page back — which a backwards walk reads as "start
    again", looping over the same rows forever."""

    @pytest.mark.asyncio
    async def test_before_id_without_before_is_an_error(self):
        from apple_mail_mcp.server import get_emails

        with pytest.raises(ValueError, match="second half of a cursor"):
            await get_emails(before_id=123)

    @pytest.mark.asyncio
    async def test_an_empty_before_is_no_before_at_all(self):
        """ "" and "   " parse to None, so checking the raw argument let
        them through and returned the newest page — the same endless
        loop, reached by the caller most likely to hit it: the one
        building the cursor from a field that came back blank."""
        from apple_mail_mcp.server import get_emails

        for blank in ("", "   "):
            with pytest.raises(ValueError, match="second half of a cursor"):
                await get_emails(before=blank, before_id=123)


class TestHiddenMailDoesNotConsumeThePage:
    """Excluded accounts are filtered in the QUERY, not afterwards.

    Dropping their rows from the result leaves them counted against
    LIMIT: if hidden mail happens to be the newest 50 messages, a caller
    asking for 50 receives an empty list, and the next page skips
    visible mail it never saw.
    """

    def test_the_query_excludes_them(self, tmp_path):
        import sqlite3

        from apple_mail_mcp.index.envelope_direct import (
            fetch_recent_messages,
        )

        path = tmp_path / "Envelope Index"
        conn = sqlite3.connect(path)
        conn.executescript("""
            CREATE TABLE messages (
                ROWID INTEGER PRIMARY KEY, message_id INTEGER,
                subject INTEGER, sender INTEGER, date_received INTEGER,
                mailbox INTEGER, read INTEGER, flagged INTEGER,
                deleted INTEGER DEFAULT 0
            );
            CREATE TABLE subjects (ROWID INTEGER PRIMARY KEY, subject TEXT);
            CREATE TABLE addresses (
                ROWID INTEGER PRIMARY KEY, address TEXT, comment TEXT
            );
            CREATE TABLE mailboxes (ROWID INTEGER PRIMARY KEY, url TEXT);
            INSERT INTO mailboxes VALUES (1, 'imap://uuid-secret/INBOX');
            INSERT INTO mailboxes VALUES (2, 'imap://uuid-work/INBOX');
            INSERT INTO addresses VALUES (1, 'a@x', '');
        """)
        # The two NEWEST messages are in the hidden account.
        for rid, mbox, ts in ((1, 1, 3000), (2, 1, 2000), (3, 2, 1000)):
            conn.execute("INSERT INTO subjects VALUES (?, ?)", (rid, f"m{rid}"))
            conn.execute(
                "INSERT INTO messages (ROWID, message_id, subject, sender,"
                " date_received, mailbox, read, flagged, deleted)"
                " VALUES (?, ?, ?, 1, ?, ?, 0, 0, 0)",
                (rid, rid, rid, ts, mbox),
            )
        conn.commit()
        conn.close()

        rows = fetch_recent_messages(
            path,
            account_uuid=None,
            mailbox_name=None,
            filter_kind="all",
            limit=2,
            exclude_account_uuids={"uuid-secret"},
        )

        assert [r.subject for r in rows] == ["m3"], (
            "hidden mail consumed the page and the visible message "
            "never appeared"
        )


class TestIncompleteWordingIsNotDoubled:
    """The message a user actually reads."""

    @pytest.mark.asyncio
    async def test_the_detail_is_not_suffixed_twice(self):
        """The JXA builder already phrases the detail as a full clause,
        so appending "not searched" produced "9 mailbox(es) not searched
        not searched"."""
        from unittest.mock import AsyncMock, MagicMock, patch

        mgr = MagicMock()
        mgr.has_index.return_value = False
        mgr.is_building.return_value = False
        mgr.write_lock_held.return_value = False
        mgr.last_error = None
        mgr.has_usable_index.return_value = False

        async def boom(script, **kw):
            raise RuntimeError(
                "Message not found with ID: 42 "
                "(INCOMPLETE: 9 mailbox(es) not searched)"
            )

        with (
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch(
                "apple_mail_mcp.server._resolve_visible_account",
                AsyncMock(return_value="Work"),
            ),
            patch(
                "apple_mail_mcp.server.execute_with_core_async",
                side_effect=boom,
            ),
        ):
            from apple_mail_mcp.server import get_email

            with pytest.raises(ValueError) as err:
                await get_email(42)

        text = str(err.value)
        assert "not searched not searched" not in text
        assert "9 mailbox(es) not searched" in text
        assert "incomplete" in text.lower()


class TestRefreshIndexTool:
    """Update the index without restarting the server.

    The index syncs at startup. Anyone searching for a message that
    arrived during the session did not find it and had no way to change
    that short of restarting the client.
    """

    @pytest.mark.asyncio
    async def test_sync_reports_the_change_count(self):
        from unittest.mock import MagicMock, patch

        mgr = MagicMock()
        mgr.has_index.return_value = True
        mgr.count_email_locations.return_value = 1
        mgr.is_building.return_value = False
        mgr.write_lock_held.return_value = False
        mgr.last_error = None
        mgr.has_usable_index.return_value = True
        mgr.sync_updates.return_value = 7

        with (
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch("apple_mail_mcp.index.disk.find_mail_directory"),
        ):
            from apple_mail_mcp.server import refresh_index

            r = await refresh_index()

        assert r["status"] == "completed"
        assert r["changes"] == 7

    @pytest.mark.asyncio
    async def test_a_failed_sync_is_not_reported_as_success(self):
        from unittest.mock import MagicMock, patch

        mgr = MagicMock()
        mgr.has_index.return_value = True
        mgr.count_email_locations.return_value = 1
        mgr.is_building.return_value = False
        mgr.write_lock_held.return_value = False
        mgr.last_error = None
        mgr.has_usable_index.return_value = True
        mgr.sync_updates.side_effect = RuntimeError("disk went away")

        with (
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch("apple_mail_mcp.index.disk.find_mail_directory"),
        ):
            from apple_mail_mcp.server import refresh_index

            r = await refresh_index()

        assert r["status"] == "failed"
        assert "disk went away" in r["error"]

    @pytest.mark.asyncio
    async def test_a_refused_rebuild_does_not_claim_it_started(self):
        """ "started" for a build refused on its first line reads as
        success and sends the caller off waiting for nothing."""
        from unittest.mock import MagicMock, patch

        mgr = MagicMock()
        mgr.has_index.return_value = True
        mgr.count_email_locations.return_value = 1
        mgr.is_building.return_value = False
        mgr.write_lock_held.return_value = False
        mgr.last_error = None
        mgr.has_usable_index.return_value = True
        mgr.build_from_disk.side_effect = RuntimeError("already running")

        with patch(
            "apple_mail_mcp.server._get_index_manager", return_value=mgr
        ):
            from apple_mail_mcp.server import refresh_index

            r = await refresh_index(full=True)

        assert r["status"] != "started"

    def test_the_docstring_claims_the_rebuild_vocabulary(self):
        """Without it a model sends the user to Mail.app's own rebuild."""
        import inspect

        from apple_mail_mcp import server

        doc = (inspect.getdoc(server.refresh_index) or "").lower()
        assert "rebuild" in doc
        assert "mailbox > rebuild" in doc or "envelope index" in doc


class TestServerLogFile:
    """The server's own log file.

    Under a desktop client stderr reaches nobody: the user sees "it
    doesn't work" and there is no record of why. A file we control is
    the only channel left.
    """

    def test_log_path_is_configurable_and_disableable(self, monkeypatch):
        from apple_mail_mcp.config import get_log_path

        monkeypatch.delenv("APPLE_MAIL_LOG_PATH", raising=False)
        assert get_log_path().name == "server.log"

        monkeypatch.setenv("APPLE_MAIL_LOG_PATH", "/tmp/custom.log")
        assert str(get_log_path()) == "/tmp/custom.log"

        # Disabled must be an explicit None: Path("") normalizes to
        # ".", which is truthy, so a plain falsy guard never fires and
        # the handler tries to open the current directory as a file.
        monkeypatch.setenv("APPLE_MAIL_LOG_PATH", "")
        assert get_log_path() is None

    def test_file_log_is_written_and_owner_only(self, tmp_path, monkeypatch):
        import logging

        from apple_mail_mcp.cli import _setup_file_logging

        target = tmp_path / "sub" / "server.log"
        monkeypatch.setenv("APPLE_MAIL_LOG_PATH", str(target))
        path = _setup_file_logging()
        try:
            logging.getLogger("apple_mail_mcp.test").info("hello from test")
            for h in logging.getLogger("apple_mail_mcp").handlers:
                h.flush()

            assert path == target
            assert target.exists()
            assert "hello from test" in target.read_text()
            assert oct(target.stat().st_mode)[-3:] == "600"
        finally:
            root = logging.getLogger("apple_mail_mcp")
            for h in list(root.handlers):
                h.close()
                root.removeHandler(h)

    def test_disabled_logging_installs_no_handler(self, monkeypatch):
        import logging

        from apple_mail_mcp.cli import _setup_file_logging

        before = len(logging.getLogger("apple_mail_mcp").handlers)
        monkeypatch.setenv("APPLE_MAIL_LOG_PATH", "")
        assert _setup_file_logging() is None
        assert len(logging.getLogger("apple_mail_mcp").handlers) == before

    def test_rotated_log_stays_owner_only(self, tmp_path, monkeypatch):
        """chmod once is not enough: on rollover the handler reopens the
        path with plain open(), i.e. 0644 under the usual umask."""
        import logging

        from apple_mail_mcp.cli import _setup_file_logging

        target = tmp_path / "server.log"
        monkeypatch.setenv("APPLE_MAIL_LOG_PATH", str(target))
        _setup_file_logging()
        root = logging.getLogger("apple_mail_mcp")
        try:
            handler = root.handlers[-1]
            handler.maxBytes = 200  # force a rollover
            log = logging.getLogger("apple_mail_mcp.rotate_test")
            for i in range(50):
                log.info("padding line %d %s", i, "x" * 40)
            handler.flush()

            assert (tmp_path / "server.log.1").exists(), "no rollover"
            for f in (target, tmp_path / "server.log.1"):
                assert oct(f.stat().st_mode)[-3:] == "600", f
        finally:
            for h in list(root.handlers):
                h.close()
                root.removeHandler(h)


class TestStatusDoesNotMisreadTheFinalPhase:
    """Two ways `get_index_status()` misread the world."""

    @pytest.mark.asyncio
    async def test_absent_mail_is_not_diagnosed_as_a_permission_problem(
        self, tmp_path
    ):
        """On a Mac where Mail was never set up there is nothing to
        read. Sending that user to Full Disk Access has them grant
        access to a directory that does not exist."""
        from unittest.mock import MagicMock, patch

        mgr = MagicMock()
        mgr.is_building.return_value = False
        mgr.has_index.return_value = False
        mgr.is_building.return_value = False
        mgr.write_lock_held.return_value = False
        mgr.last_error = None
        mgr.has_usable_index.return_value = False
        mgr.indexed_email_count.return_value = 0
        mgr.last_error = None
        mgr.recent_events.return_value = []

        with (
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch(
                "apple_mail_mcp.index.disk.find_mail_directory",
                side_effect=FileNotFoundError("no ~/Library/Mail"),
            ),
        ):
            from apple_mail_mcp.server import get_index_status

            r = await get_index_status()

        assert "Full Disk Access" not in r["problem"]
        assert "not a permissions problem" in r["user_message"]

    def test_the_build_flag_outlives_the_bulk_loop(self, tmp_path):
        """The final flush and the FTS rebuild are the heaviest writes in
        the program. Reporting "ready" during them also lets a fresh disk
        walk start against a database still being rewritten."""
        from unittest.mock import patch

        from apple_mail_mcp.index import IndexManager

        mgr = IndexManager(db_path=tmp_path / "idx.db")
        seen: list[bool] = []

        def one_email(*a, **kw):
            yield {
                "id": 1,
                "account": "acct",
                "mailbox": "INBOX",
                "subject": "s",
                "sender": "a@x",
                "content": "body",
                "date_received": "2026-07-28",
                "emlx_path": "/tmp/1.emlx",
                "attachments": [],
            }

        real_rebuild = None

        def spy(conn):
            # Called during finalization — the build must still say so.
            seen.append(mgr.is_building())
            return real_rebuild(conn)

        from apple_mail_mcp.index import manager as mgr_mod

        real_rebuild = mgr_mod.rebuild_fts_index

        with (
            patch(
                "apple_mail_mcp.index.disk.find_mail_directory",
                return_value=tmp_path,
            ),
            patch(
                "apple_mail_mcp.index.disk.scan_all_emails",
                side_effect=one_email,
            ),
            patch.object(mgr, "_resolve_exclusions", return_value=set()),
            patch.object(mgr_mod, "rebuild_fts_index", side_effect=spy),
        ):
            mgr.build_from_disk()

        assert seen == [True], "status said 'ready' mid-finalization"
        assert mgr.is_building() is False


@pytest.mark.skipif(
    shutil.which("osascript") is None,
    reason="drives the real JXA overlay; needs macOS",
)
class TestStrategy0OverlaysFlagsEndToEnd:
    """Testing the helper is not testing the tool.

    Every existing test here drives `_overlay_live_flags()` directly, so
    deleting the single `await _overlay_live_flags(...)` call inside
    `get_email()` leaves them all green while the tool goes back to
    reporting whatever the `.emlx` footer said.
    """

    @pytest.mark.asyncio
    async def test_get_email_returns_the_live_flags(self, tmp_path):
        from unittest.mock import AsyncMock, MagicMock, patch

        emlx = tmp_path / "42.emlx"
        emlx.write_bytes(b"stub")

        parsed = MagicMock()
        parsed.id = 42
        parsed.subject = "s"
        parsed.sender = "a@x"
        parsed.content = "body"
        parsed.date_received = "2026-07-28T10:00:00"
        parsed.date_sent = "2026-07-28T09:00:00"
        parsed.reply_to = ""
        parsed.message_id_header = "<a@x>"
        parsed.attachments = []
        # What the footer claims — stale.
        parsed.read = False
        parsed.flagged = False

        mgr = MagicMock()
        mgr.has_index.return_value = True
        mgr.count_email_locations.return_value = 1
        mgr.is_building.return_value = False
        mgr.write_lock_held.return_value = False
        mgr.last_error = None
        mgr.has_usable_index.return_value = True
        mgr.find_email_path.return_value = emlx
        amap = MagicMock()
        amap.ensure_loaded = AsyncMock()
        amap.name_to_uuid.return_value = "uuid-work"
        amap.names_to_uuids.return_value = set()

        with (
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch("apple_mail_mcp.server._get_account_map", return_value=amap),
            patch("apple_mail_mcp.index.disk.parse_emlx", return_value=parsed),
            patch(
                "apple_mail_mcp.index.envelope_direct.fetch_message_flags",
                # What Mail actually knows.
                return_value=(True, True),
            ),
            patch(
                "apple_mail_mcp.index.envelope_direct.envelope_index_path",
                return_value=MagicMock(exists=lambda: True),
            ),
            patch(
                "apple_mail_mcp.index.disk.find_mail_directory",
                return_value=tmp_path,
            ),
        ):
            from apple_mail_mcp.server import get_email

            out = await get_email(42, account="Work", mailbox="INBOX")

        assert out["read"] is True, "footer said unread; Mail says read"
        assert out["flagged"] is True


class TestTheDocumentedToolCountMatchesReality:
    """Six shipped files state the number. A PR that adds a tool and
    updates one of them leaves five lying to the reader."""

    def test_no_document_still_claims_the_old_count(self):
        from pathlib import Path

        stale = []
        for name in (
            "README.md",
            "CLAUDE.md",
            "CONTRIBUTING.md",
            "docs/index.md",
            "docs/tools.md",
            "docs/architecture.md",
            "docs/getting-started.md",
        ):
            p = Path(name)
            if not p.exists():
                continue
            text = p.read_text()
            for claim in ("8 MCP tools", "all 8 tools", "8 tools for"):
                if claim in text:
                    stale.append(f"{name}: {claim}")
        assert not stale, stale


class TestTheLogFileModeIsEnforcedNotJustRequested:
    """`os.open(..., 0o600)` applies the mode only when it CREATES the
    file. An existing `server.log` — written before this feature, or by
    a differently-umasked run — kept whatever mode it had, so the very
    file the guard exists to protect stayed world-readable."""

    def test_an_existing_world_readable_log_is_tightened(
        self, tmp_path, monkeypatch
    ):
        import logging
        import os

        from apple_mail_mcp.cli import _setup_file_logging

        target = tmp_path / "server.log"
        target.write_text("from an earlier run\n")
        os.chmod(target, 0o644)

        monkeypatch.setenv("APPLE_MAIL_LOG_PATH", str(target))
        _setup_file_logging()
        root = logging.getLogger("apple_mail_mcp")
        try:
            assert oct(target.stat().st_mode)[-3:] == "600"
        finally:
            for h in list(root.handlers):
                h.close()
                root.removeHandler(h)

    def test_index_and_rebuild_set_up_logging(self):
        """A failing build is what the log exists for, and those are the
        commands that run one."""
        import ast
        import inspect

        from apple_mail_mcp import cli

        tree = ast.parse(inspect.getsource(cli))
        for name in ("index", "rebuild", "_run_serve"):
            fn = next(
                n
                for n in tree.body
                if isinstance(n, ast.FunctionDef) and n.name == name
            )
            calls = {
                c.func.id
                for c in ast.walk(fn)
                if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
            }
            assert "_setup_file_logging" in calls, name


class TestOneIndexWriterAtATime:
    """The branch this came from guarded rebuilds with a module-level
    flag. Here the manager's cross-process WriteLock does it, so the
    claim is the same and the mechanism is stronger — but it has to be
    asserted against the mechanism that actually exists."""

    @pytest.mark.asyncio
    async def test_a_refresh_during_a_write_is_refused(self):
        from unittest.mock import MagicMock, patch

        import apple_mail_mcp.server as srv

        mgr = MagicMock()
        mgr.has_index.return_value = True
        mgr.count_email_locations.return_value = 1
        mgr.has_usable_index.return_value = True
        mgr.is_building.return_value = False
        mgr.write_lock_held.return_value = True  # a build holds it
        mgr.last_error = None

        with patch.object(srv, "_get_index_manager", return_value=mgr):
            r = await srv.refresh_index()

        assert r["status"] == "already_running"
        mgr.sync_updates.assert_not_called()
        mgr.build_from_disk.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_busy_index_is_not_reported_as_a_failed_sync(self):
        """IndexBusyError is the manager saying "someone else is
        writing", not a broken index — reporting it as `failed` sends
        the user looking for a defect that is not there."""
        from unittest.mock import MagicMock, patch

        import apple_mail_mcp.server as srv
        from apple_mail_mcp.index.manager import IndexBusyError

        mgr = MagicMock()
        mgr.has_index.return_value = True
        mgr.count_email_locations.return_value = 1
        mgr.has_usable_index.return_value = True
        mgr.is_building.return_value = False
        mgr.write_lock_held.return_value = False
        mgr.last_error = None
        mgr.sync_updates.side_effect = IndexBusyError("busy")

        with (
            patch.object(srv, "_get_index_manager", return_value=mgr),
            patch("apple_mail_mcp.index.disk.find_mail_directory"),
        ):
            r = await srv.refresh_index()

        assert r["status"] == "already_running"


class TestAColdAccountCacheDoesNotWidenTheQuery:
    """A bare `get_emails()` means "the default account". When the
    AccountMap cache is empty — `ensure_loaded()` talks to Mail, and it
    can be slow under a batch of concurrent calls, or refused — the
    resolved UUID stayed None and the Envelope query ran UNSCOPED. The
    listing then answered for every account, which is a different
    question from the one that was asked, and it looked like a correct
    answer. Reported from the field: a bare get_emails() issued
    alongside other calls returned mail from an account the caller had
    not named, while the same call on its own did not."""

    @pytest.mark.asyncio
    async def test_an_empty_cache_falls_back_instead_of_querying_all(self):
        from unittest.mock import AsyncMock, MagicMock, patch

        import apple_mail_mcp.server as srv

        acct_map = _acct_map()
        acct_map.get_cached_accounts.return_value = []  # cold / failed

        fetch = MagicMock(return_value=[])
        with (
            patch.object(srv, "_get_account_map", return_value=acct_map),
            patch.object(
                srv, "_resolve_visible_account", AsyncMock(return_value=None)
            ),
            patch.object(srv, "_excluded_account_names", return_value=set()),
            patch(
                "apple_mail_mcp.index.envelope_direct.fetch_recent_messages",
                fetch,
            ),
            patch(
                "apple_mail_mcp.index.envelope_direct.envelope_index_path"
            ) as env_path,
            patch("apple_mail_mcp.index.disk.find_mail_directory"),
            patch.object(
                srv, "execute_query_async", AsyncMock(return_value=[])
            ),
        ):
            env_path.return_value.exists.return_value = True
            await srv.get_emails(limit=5)

        for call in fetch.call_args_list:
            assert call.kwargs.get("account_uuid") is not None, (
                "a cold cache ran an unscoped query — the listing "
                "silently covered every account"
            )


class TestTheStatusSaysWhetherTheLogIsBeingWritten:
    """A client with no filesystem access saw the log PATH and nothing
    else, so "logging is configured" and "logging works" looked the
    same. Reported from the field. The contents stay on disk — log
    lines carry mail subjects and file paths."""

    def test_a_written_log_reports_its_size_and_mtime(
        self, tmp_path, monkeypatch
    ):
        from apple_mail_mcp.server import _log_file_facts

        target = tmp_path / "server.log"
        target.write_text("2026-08-11 INFO apple_mail_mcp: hello\n")
        monkeypatch.setenv("APPLE_MAIL_LOG_PATH", str(target))

        facts = _log_file_facts()
        assert facts["log_file_exists"] is True
        assert facts["log_file_bytes"] > 0
        assert facts["log_file_modified"]

    def test_a_missing_log_says_so_instead_of_raising(
        self, tmp_path, monkeypatch
    ):
        from apple_mail_mcp.server import _log_file_facts

        monkeypatch.setenv(
            "APPLE_MAIL_LOG_PATH", str(tmp_path / "nope" / "server.log")
        )
        assert _log_file_facts() == {"log_file_exists": False}

    def test_no_log_content_is_exposed(self):
        import inspect

        from apple_mail_mcp import server

        src = inspect.getsource(server._log_file_facts)
        for leak in ("read_text", "read_bytes", "readlines", "open("):
            assert leak not in src, (
                f"{leak} in the status path would ship log CONTENT — "
                f"subjects and file paths — to the caller"
            )


class TestTheScanReportsWhereItFoundTheMessage:
    """Strategy 3 answers precisely when the index does NOT know the
    message — a mail that just arrived, or one another device just
    moved. That is the one case where the caller cannot derive the
    location, and it was the only return path that dropped it: the JXA
    script had the mailbox in hand and returned neither it nor the
    account. CLAUDE.md promises `get_email` reports the current
    location. Reported from the field."""

    def test_the_generated_script_returns_the_location(self):
        from apple_mail_mcp.builders import GetEmailBuilder

        js = GetEmailBuilder(
            message_id=1, account="Work", max_mailboxes=5, attachment_js="[]"
        ).build()
        assert "mailbox: foundMailbox" in js
        assert "account: accountName" in js
        assert "foundMailbox = String(mb.name())" in js

    @pytest.mark.asyncio
    async def test_the_tool_passes_it_through(self):
        from unittest.mock import AsyncMock, patch

        import apple_mail_mcp.server as srv

        # The script reports the mailbox it found; the ACCOUNT has to
        # be filled in by _with_location, which this return path used
        # not to call. Leaving it out of the payload is what makes this
        # test fail without the fix instead of passing through.
        found = {
            "id": 1,
            "subject": "x",
            "message_id": "a@b",
            "mailbox": "Junk",
        }
        with (
            patch.object(srv, "_get_index_manager") as mgr,
            patch.object(
                srv,
                "_resolve_visible_account",
                AsyncMock(return_value="iCloud"),
            ),
            patch.object(srv, "_excluded_account_names", return_value=set()),
            patch.object(
                srv,
                "execute_with_core_async",
                AsyncMock(side_effect=[Exception("s1"), found]),
            ),
            patch.object(
                srv, "execute_query_async", AsyncMock(return_value=[])
            ),
        ):
            mgr.return_value.has_index.return_value = False
            out = await srv.get_email(1)

        assert out["mailbox"] == "Junk"
        assert out["account"] == "iCloud"


class TestOneSpellingOfTheHeaderWhicheverStrategyAnswered:
    """The `.emlx` keeps the angle brackets, Apple's `messageId` drops
    them — so the same message came back as "<a@b>" from disk and "a@b"
    from JXA, while search() and get_emails() always say "<a@b>". A
    caller comparing strings saw two different messages. Reported from
    the field."""

    @pytest.mark.asyncio
    async def test_a_jxa_answer_is_bracketed_like_the_index(self):
        from unittest.mock import AsyncMock, patch

        import apple_mail_mcp.server as srv

        bare = {"id": 1, "subject": "x", "message_id": "a@b"}
        with (
            patch.object(srv, "_get_index_manager") as mgr,
            patch.object(
                srv,
                "_resolve_visible_account",
                AsyncMock(return_value="iCloud"),
            ),
            patch.object(srv, "_excluded_account_names", return_value=set()),
            patch.object(
                srv,
                "execute_with_core_async",
                AsyncMock(side_effect=[Exception("s1"), bare]),
            ),
            patch.object(
                srv, "execute_query_async", AsyncMock(return_value=[])
            ),
        ):
            mgr.return_value.has_index.return_value = False
            out = await srv.get_email(1)

        assert out["message_id"] == "<a@b>"

    @pytest.mark.asyncio
    async def test_an_already_bracketed_header_is_left_alone(self):
        from unittest.mock import AsyncMock, patch

        import apple_mail_mcp.server as srv

        already = {"id": 1, "subject": "x", "message_id": "<a@b>"}
        with (
            patch.object(srv, "_get_index_manager") as mgr,
            patch.object(
                srv,
                "_resolve_visible_account",
                AsyncMock(return_value="iCloud"),
            ),
            patch.object(srv, "_excluded_account_names", return_value=set()),
            patch.object(
                srv,
                "execute_with_core_async",
                AsyncMock(side_effect=[Exception("s1"), already]),
            ),
            patch.object(
                srv, "execute_query_async", AsyncMock(return_value=[])
            ),
        ):
            mgr.return_value.has_index.return_value = False
            out = await srv.get_email(1)

        assert out["message_id"] == "<a@b>"
