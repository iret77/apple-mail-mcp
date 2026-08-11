"""Tests for IndexManager class.

Tests the central orchestration class for the FTS5 search index:
- Singleton pattern
- Index existence checking
- Sync operations
- Staleness detection
- Search delegation
- Statistics
"""

from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from apple_mail_mcp.index.manager import IndexManager, IndexStats


class TestIndexManagerSingleton:
    """Tests for singleton pattern."""

    def teardown_method(self):
        """Reset singleton after each test."""
        IndexManager._instance = None

    def test_get_instance_returns_same_object(self):
        """get_instance returns the same IndexManager object."""
        m1 = IndexManager.get_instance()
        m2 = IndexManager.get_instance()
        assert m1 is m2

    def test_reset_clears_singleton(self):
        """Resetting _instance creates new manager."""
        m1 = IndexManager.get_instance()
        IndexManager._instance = None
        m2 = IndexManager.get_instance()
        assert m1 is not m2

    def test_custom_db_path_is_used(self, tmp_path):
        """Custom db_path is used when provided."""
        custom_path = tmp_path / "custom.db"
        manager = IndexManager(db_path=custom_path)
        assert manager.db_path == custom_path


class TestHasIndex:
    """Tests for index existence checking."""

    def teardown_method(self):
        IndexManager._instance = None

    @pytest.mark.parametrize(
        "file_exists, expected", [(False, False), (True, True)]
    )
    def test_has_index_reflects_db_existence(
        self, tmp_path, file_exists, expected
    ):
        """has_index returns True iff the database file exists."""
        db_path = tmp_path / "test.db"
        if file_exists:
            db_path.touch()
        manager = IndexManager(db_path=db_path)
        assert manager.has_index() is expected


class TestGetStats:
    """Tests for index statistics."""

    def teardown_method(self):
        IndexManager._instance = None

    def test_get_stats_returns_index_stats(self, temp_db_path):
        """get_stats returns IndexStats dataclass."""
        manager = IndexManager(db_path=temp_db_path)

        # Initialize the database by getting connection
        conn = manager._get_conn()
        conn.execute(
            "INSERT INTO emails (message_id, account, mailbox, subject) "
            "VALUES (1, 'test', 'INBOX', 'Test')"
        )
        conn.commit()

        stats = manager.get_stats()

        assert isinstance(stats, IndexStats)
        assert stats.email_count == 1
        assert stats.mailbox_count == 1

    def test_get_stats_reports_zero_for_empty_index(self, temp_db_path):
        """get_stats reports zero counts for empty index."""
        manager = IndexManager(db_path=temp_db_path)
        manager._get_conn()  # Initialize DB

        stats = manager.get_stats()

        assert stats.email_count == 0
        assert stats.mailbox_count == 0
        assert stats.last_sync is None

    def test_get_stats_calculates_staleness(self, temp_db_path):
        """get_stats calculates staleness hours from last_sync."""
        manager = IndexManager(db_path=temp_db_path)
        conn = manager._get_conn()

        # Set a sync time 2 hours ago
        two_hours_ago = (datetime.now() - timedelta(hours=2)).isoformat()
        conn.execute(
            "INSERT INTO sync_state (account, mailbox, last_sync) "
            "VALUES ('test', 'INBOX', ?)",
            (two_hours_ago,),
        )
        conn.commit()

        stats = manager.get_stats()

        assert stats.staleness_hours is not None
        assert 1.9 < stats.staleness_hours < 2.1  # Allow small timing variance


class TestIsStale:
    """Tests for staleness detection."""

    def teardown_method(self):
        IndexManager._instance = None

    def test_is_stale_returns_true_when_never_synced(self, temp_db_path):
        """is_stale returns True when no sync has occurred."""
        manager = IndexManager(db_path=temp_db_path)
        manager._get_conn()  # Initialize DB

        assert manager.is_stale() is True

    def test_is_stale_returns_true_when_old(self, temp_db_path):
        """is_stale returns True when last sync exceeds threshold."""
        manager = IndexManager(db_path=temp_db_path)
        conn = manager._get_conn()

        # Set sync time beyond default staleness threshold (24h)
        old_time = (datetime.now() - timedelta(hours=25)).isoformat()
        conn.execute(
            "INSERT INTO sync_state (account, mailbox, last_sync) "
            "VALUES ('test', 'INBOX', ?)",
            (old_time,),
        )
        conn.commit()

        assert manager.is_stale() is True

    def test_is_stale_returns_false_when_recent(self, temp_db_path):
        """is_stale returns False when last sync is recent."""
        manager = IndexManager(db_path=temp_db_path)
        conn = manager._get_conn()

        # Set recent sync time
        recent_time = datetime.now().isoformat()
        conn.execute(
            "INSERT INTO sync_state (account, mailbox, last_sync) "
            "VALUES ('test', 'INBOX', ?)",
            (recent_time,),
        )
        conn.commit()

        assert manager.is_stale() is False


class TestSyncUpdates:
    """Tests for disk-based sync."""

    def teardown_method(self):
        IndexManager._instance = None

    @patch("apple_mail_mcp.index.sync.sync_from_disk")
    @patch("apple_mail_mcp.index.disk.find_mail_directory")
    def test_sync_updates_calls_disk_sync(
        self, mock_find, mock_sync, temp_db_path
    ):
        """sync_updates calls sync_from_disk with correct args."""
        mock_find.return_value = Path("/fake/mail")
        mock_result = MagicMock()
        mock_result.total_changes = 5
        mock_sync.return_value = mock_result

        manager = IndexManager(db_path=temp_db_path)
        result = manager.sync_updates()

        assert result == 5
        mock_sync.assert_called_once()

    @pytest.mark.parametrize("error_cls", [FileNotFoundError, PermissionError])
    @patch("apple_mail_mcp.index.disk.find_mail_directory")
    def test_sync_updates_handles_inaccessible_mail_dir(
        self, mock_find, error_cls, temp_db_path
    ):
        """sync_updates returns 0 when mail directory is inaccessible."""
        mock_find.side_effect = error_cls("Cannot access")

        manager = IndexManager(db_path=temp_db_path)
        assert manager.sync_updates() == 0


class TestSearch:
    """Tests for search delegation."""

    def teardown_method(self):
        IndexManager._instance = None

    @patch("apple_mail_mcp.index.search.search_fts")
    def test_search_delegates_to_search_fts(self, mock_search, temp_db_path):
        """search delegates to search_fts function."""
        mock_search.return_value = []

        manager = IndexManager(db_path=temp_db_path)
        manager._get_conn()  # Initialize connection

        manager.search("invoice", account="Work", mailbox="INBOX", limit=10)

        mock_search.assert_called_once()
        call_args = mock_search.call_args
        assert call_args[0][1] == "invoice"  # query
        assert call_args[1]["account"] == "Work"
        assert call_args[1]["mailbox"] == "INBOX"
        assert call_args[1]["limit"] == 10


class TestClose:
    """Tests for connection management."""

    def teardown_method(self):
        IndexManager._instance = None

    def test_close_is_idempotent(self, temp_db_path):
        """close() releases every connection and can be repeated."""
        manager = IndexManager(db_path=temp_db_path)
        manager._get_conn()
        assert manager._open_conns

        manager.close()
        assert not manager._open_conns

        manager.close()  # Should not raise
        assert not manager._open_conns

        # A later caller simply gets a fresh connection.
        assert manager._get_conn() is not None


class TestGetIndexedMessageIds:
    """Tests for get_indexed_message_ids."""

    def teardown_method(self):
        IndexManager._instance = None

    def test_returns_empty_set_when_no_emails(self, temp_db_path):
        """get_indexed_message_ids returns empty set for empty index."""
        manager = IndexManager(db_path=temp_db_path)
        manager._get_conn()

        ids = manager.get_indexed_message_ids()

        assert ids == set()

    def test_returns_all_message_ids(self, temp_db_path):
        """get_indexed_message_ids returns all IDs when no filter."""
        manager = IndexManager(db_path=temp_db_path)
        conn = manager._get_conn()

        # Insert test emails
        conn.execute(
            "INSERT INTO emails (message_id, account, mailbox) "
            "VALUES (1, 'a', 'm1'), (2, 'a', 'm1'), (3, 'b', 'm2')"
        )
        conn.commit()

        ids = manager.get_indexed_message_ids()

        assert ids == {1, 2, 3}

    def test_filters_by_account(self, temp_db_path):
        """get_indexed_message_ids filters by account."""
        manager = IndexManager(db_path=temp_db_path)
        conn = manager._get_conn()

        conn.execute(
            "INSERT INTO emails (message_id, account, mailbox) "
            "VALUES (1, 'a', 'm1'), (2, 'a', 'm1'), (3, 'b', 'm2')"
        )
        conn.commit()

        ids = manager.get_indexed_message_ids(account="a")

        assert ids == {1, 2}

    def test_filters_by_account_and_mailbox(self, temp_db_path):
        """get_indexed_message_ids filters by both account and mailbox."""
        manager = IndexManager(db_path=temp_db_path)
        conn = manager._get_conn()

        conn.execute(
            "INSERT INTO emails (message_id, account, mailbox) "
            "VALUES (1, 'a', 'm1'), (2, 'a', 'm2'), (3, 'b', 'm1')"
        )
        conn.commit()

        ids = manager.get_indexed_message_ids(account="a", mailbox="m1")

        assert ids == {1}


class TestFindEmailLocation:
    """Tests for find_email_location (#37)."""

    def teardown_method(self):
        IndexManager._instance = None

    def test_found(self, temp_db_path):
        manager = IndexManager(db_path=temp_db_path)
        conn = manager._get_conn()
        conn.execute(
            "INSERT INTO emails (message_id, account, mailbox) "
            "VALUES (42, 'uuid-1', 'INBOX')"
        )
        conn.commit()

        result = manager.find_email_location(42)
        assert result == ("uuid-1", "INBOX")

    def test_not_found(self, temp_db_path):
        manager = IndexManager(db_path=temp_db_path)
        manager._get_conn()

        assert manager.find_email_location(999) is None

    def test_with_filters(self, temp_db_path):
        manager = IndexManager(db_path=temp_db_path)
        conn = manager._get_conn()
        conn.execute(
            "INSERT INTO emails (message_id, account, mailbox) "
            "VALUES (42, 'uuid-1', 'INBOX')"
        )
        conn.execute(
            "INSERT INTO emails (message_id, account, mailbox) "
            "VALUES (42, 'uuid-2', 'Sent')"
        )
        conn.commit()

        result = manager.find_email_location(
            42, account="uuid-2", mailbox="Sent"
        )
        assert result == ("uuid-2", "Sent")


class TestFindEmailPath:
    """Tests for find_email_path (#37)."""

    def teardown_method(self):
        IndexManager._instance = None

    def test_found(self, temp_db_path):
        manager = IndexManager(db_path=temp_db_path)
        conn = manager._get_conn()
        conn.execute(
            "INSERT INTO emails "
            "(message_id, account, mailbox, emlx_path) "
            "VALUES (42, 'uuid-1', 'INBOX', '/path/to/42.emlx')"
        )
        conn.commit()

        result = manager.find_email_path(42)
        assert result is not None
        assert str(result) == "/path/to/42.emlx"

    def test_null_path(self, temp_db_path):
        manager = IndexManager(db_path=temp_db_path)
        conn = manager._get_conn()
        conn.execute(
            "INSERT INTO emails (message_id, account, mailbox) "
            "VALUES (42, 'uuid-1', 'INBOX')"
        )
        conn.commit()

        assert manager.find_email_path(42) is None


class TestDeleteEmail:
    """Tests for delete_email (#74)."""

    def teardown_method(self):
        IndexManager._instance = None

    def test_deletes_matching_row(self, temp_db_path):
        manager = IndexManager(db_path=temp_db_path)
        conn = manager._get_conn()
        conn.execute(
            "INSERT INTO emails "
            "(message_id, account, mailbox, subject, sender, content) "
            "VALUES (42, 'uuid-1', 'INBOX', 'Hello', 'a@b.com', 'body')"
        )
        conn.commit()

        deleted = manager.delete_email(42)

        assert deleted == 1
        assert manager.find_email_path(42) is None
        # FTS5 row should be gone too via the AFTER DELETE trigger
        fts_count = conn.execute(
            "SELECT COUNT(*) AS n FROM emails_fts WHERE subject MATCH 'Hello'"
        ).fetchone()["n"]
        assert fts_count == 0

    def test_returns_zero_when_no_match(self, temp_db_path):
        manager = IndexManager(db_path=temp_db_path)
        assert manager.delete_email(999) == 0

    def test_scopes_by_account_and_mailbox(self, temp_db_path):
        manager = IndexManager(db_path=temp_db_path)
        conn = manager._get_conn()
        conn.execute(
            "INSERT INTO emails (message_id, account, mailbox) "
            "VALUES (42, 'uuid-A', 'INBOX')"
        )
        conn.execute(
            "INSERT INTO emails (message_id, account, mailbox) "
            "VALUES (42, 'uuid-B', 'INBOX')"
        )
        conn.commit()

        deleted = manager.delete_email(42, account="uuid-A", mailbox="INBOX")

        assert deleted == 1
        # The uuid-B row survives
        remaining = conn.execute(
            "SELECT account FROM emails WHERE message_id = 42"
        ).fetchall()
        assert len(remaining) == 1
        assert remaining[0]["account"] == "uuid-B"


class TestBuildFromDiskTriggers:
    """Verify FTS5 triggers are reactivated after build_from_disk.

    Regression for the watcher race: if triggers were recreated AFTER
    rebuild_fts_index (the old order), any INSERT that landed between
    rebuild and recreation would never enter emails_fts. The reorder
    in build_from_disk fixes that — this test verifies the invariant
    that any INSERT after build_from_disk fires the trigger.
    """

    def teardown_method(self):
        IndexManager._instance = None

    def test_after_insert_trigger_active_post_build(
        self, tmp_path, temp_db_path, monkeypatch
    ):
        manager = IndexManager(db_path=temp_db_path)

        # Empty mail dir — build_from_disk traverses nothing.
        empty_mail = tmp_path / "Mail" / "V10"
        empty_mail.mkdir(parents=True)
        monkeypatch.setattr(
            "apple_mail_mcp.index.disk.find_mail_directory",
            lambda: empty_mail,
        )
        manager.build_from_disk()

        conn = manager._get_conn()

        # Post-build INSERT should fire the AFTER INSERT trigger and
        # land in emails_fts.
        conn.execute(
            "INSERT INTO emails "
            "(message_id, account, mailbox, subject, sender, content) "
            "VALUES (1, 'acc', 'INBOX', 'Hello world', 's@x.com', 'Body')"
        )
        conn.commit()

        match_count = conn.execute(
            "SELECT COUNT(*) AS n FROM emails_fts "
            "WHERE emails_fts MATCH 'Hello'"
        ).fetchone()["n"]
        assert match_count == 1, (
            "AFTER INSERT trigger missing — emails_fts not populated. "
            "Likely the trigger recreation order regressed."
        )

    def test_after_delete_trigger_active_post_build(
        self, tmp_path, temp_db_path, monkeypatch
    ):
        manager = IndexManager(db_path=temp_db_path)

        empty_mail = tmp_path / "Mail" / "V10"
        empty_mail.mkdir(parents=True)
        monkeypatch.setattr(
            "apple_mail_mcp.index.disk.find_mail_directory",
            lambda: empty_mail,
        )
        manager.build_from_disk()

        conn = manager._get_conn()
        conn.execute(
            "INSERT INTO emails "
            "(message_id, account, mailbox, subject, sender, content) "
            "VALUES (2, 'acc', 'INBOX', 'Goodbye', 's@x.com', 'Body')"
        )
        conn.execute("DELETE FROM emails WHERE message_id = 2")
        conn.commit()

        match_count = conn.execute(
            "SELECT COUNT(*) AS n FROM emails_fts "
            "WHERE emails_fts MATCH 'Goodbye'"
        ).fetchone()["n"]
        assert match_count == 0, (
            "AFTER DELETE trigger missing — emails_fts retained a deleted row."
        )

    def test_triggers_present_when_rebuild_fts_runs(
        self, tmp_path, temp_db_path, monkeypatch
    ):
        """Order regression: triggers must be recreated BEFORE
        rebuild_fts_index runs. If the order is reversed, any
        concurrent INSERT during the rebuild window lands in `emails`
        but never reaches `emails_fts`.
        """
        manager = IndexManager(db_path=temp_db_path)

        # Fake mail dir with one valid .emlx so total_indexed > 0
        # and rebuild_fts_index actually runs.
        mail_dir = tmp_path / "Mail" / "V10"
        mbox = mail_dir / "acc1" / "INBOX.mbox" / "Data" / "Messages"
        mbox.mkdir(parents=True)
        emlx = mbox / "1001.emlx"
        emlx.write_bytes(b"100\nFrom: t@t.com\nSubject: Test\n\nBody")

        monkeypatch.setattr(
            "apple_mail_mcp.index.disk.find_mail_directory",
            lambda: mail_dir,
        )

        triggers_at_rebuild_time: list[str] = []

        def hook_rebuild(conn):
            rows = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='trigger' "
                "AND name IN ('emails_ai', 'emails_ad', 'emails_au')"
            ).fetchall()
            triggers_at_rebuild_time.extend(r[0] for r in rows)
            # Skip the actual rebuild — we only want to inspect state.

        monkeypatch.setattr(
            "apple_mail_mcp.index.manager.rebuild_fts_index",
            hook_rebuild,
        )

        manager.build_from_disk()

        assert "emails_ai" in triggers_at_rebuild_time, (
            "AFTER INSERT trigger missing when rebuild_fts_index ran "
            "— trigger recreation order regressed."
        )
        assert "emails_ad" in triggers_at_rebuild_time
        assert "emails_au" in triggers_at_rebuild_time


class TestParseFailureDLQ:
    """Tests for record_parse_failure / clear_parse_failure (#58)."""

    def teardown_method(self):
        IndexManager._instance = None

    def test_record_inserts_new_failure(self, temp_db_path):
        manager = IndexManager(db_path=temp_db_path)

        manager.record_parse_failure(
            "/Library/Mail/V10/uuid/INBOX/Messages/42.emlx",
            "uuid-1",
            "INBOX",
            ValueError("malformed plist"),
        )

        conn = manager._get_conn()
        row = conn.execute(
            "SELECT account, mailbox, error_type, error_message, "
            "attempt_count FROM failed_index_jobs"
        ).fetchone()
        assert row["account"] == "uuid-1"
        assert row["mailbox"] == "INBOX"
        assert row["error_type"] == "ValueError"
        assert row["error_message"] == "malformed plist"
        assert row["attempt_count"] == 1

    def test_record_idempotent_increments_attempt_count(self, temp_db_path):
        manager = IndexManager(db_path=temp_db_path)
        path = "/path/42.emlx"

        manager.record_parse_failure(
            path, "uuid-1", "INBOX", ValueError("first")
        )
        manager.record_parse_failure(path, "uuid-1", "INBOX", OSError("second"))
        manager.record_parse_failure(path, "uuid-1", "INBOX", OSError("third"))

        conn = manager._get_conn()
        row = conn.execute(
            "SELECT attempt_count, error_type, error_message, "
            "first_seen, last_seen FROM failed_index_jobs"
        ).fetchone()
        assert row["attempt_count"] == 3
        # Latest error wins on type/message; first_seen survives
        assert row["error_type"] == "OSError"
        assert row["error_message"] == "third"

    def test_record_truncates_long_messages(self, temp_db_path):
        manager = IndexManager(db_path=temp_db_path)
        long_msg = "x" * 1000

        manager.record_parse_failure(
            "/path/42.emlx",
            "uuid-1",
            "INBOX",
            ValueError(long_msg),
        )

        conn = manager._get_conn()
        stored = conn.execute(
            "SELECT error_message FROM failed_index_jobs"
        ).fetchone()["error_message"]
        assert len(stored) == 500

    def test_clear_removes_entry(self, temp_db_path):
        manager = IndexManager(db_path=temp_db_path)
        path = "/path/42.emlx"
        manager.record_parse_failure(
            path, "uuid-1", "INBOX", ValueError("oops")
        )

        deleted = manager.clear_parse_failure(path)

        assert deleted == 1
        conn = manager._get_conn()
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM failed_index_jobs"
        ).fetchone()["n"]
        assert count == 0

    def test_clear_returns_zero_when_absent(self, temp_db_path):
        manager = IndexManager(db_path=temp_db_path)
        assert manager.clear_parse_failure("/never/seen.emlx") == 0

    def test_get_stats_includes_failed_jobs_count(self, temp_db_path):
        manager = IndexManager(db_path=temp_db_path)
        manager.record_parse_failure("/a.emlx", "u", "INBOX", ValueError("a"))
        manager.record_parse_failure("/b.emlx", "u", "INBOX", ValueError("b"))

        stats = manager.get_stats()
        assert stats.failed_jobs_count == 2


class TestSearchAttachments:
    """Tests for search_attachments (#37)."""

    def teardown_method(self):
        IndexManager._instance = None

    def test_basic(self, temp_db_path):
        manager = IndexManager(db_path=temp_db_path)
        conn = manager._get_conn()
        conn.execute(
            "INSERT INTO emails "
            "(message_id, account, mailbox, subject, sender, "
            "date_received, attachment_count) "
            "VALUES (1, 'acc', 'INBOX', 'Test', 'a@b.com', "
            "'2024-01-01', 1)"
        )
        rowid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO attachments "
            "(email_rowid, filename, mime_type, file_size) "
            "VALUES (?, 'invoice.pdf', 'application/pdf', 100)",
            (rowid,),
        )
        conn.commit()

        results = manager.search_attachments("invoice")
        assert len(results) == 1
        assert results[0]["filename"] == "invoice.pdf"

    def test_with_filters(self, temp_db_path):
        manager = IndexManager(db_path=temp_db_path)
        conn = manager._get_conn()
        conn.execute(
            "INSERT INTO emails "
            "(message_id, account, mailbox, subject, sender, "
            "date_received, attachment_count) "
            "VALUES (1, 'acc1', 'INBOX', 'Test', 'a@b.com', "
            "'2024-01-01', 1)"
        )
        rowid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO attachments "
            "(email_rowid, filename) VALUES (?, 'doc.pdf')",
            (rowid,),
        )
        conn.commit()

        # Should find with matching account
        results = manager.search_attachments("doc", account="acc1")
        assert len(results) == 1

        # Should not find with wrong account
        results = manager.search_attachments("doc", account="other")
        assert len(results) == 0


class TestGetEmailAttachments:
    """Tests for get_email_attachments (#36)."""

    def teardown_method(self):
        IndexManager._instance = None

    def test_found(self, temp_db_path):
        manager = IndexManager(db_path=temp_db_path)
        conn = manager._get_conn()
        conn.execute(
            "INSERT INTO emails "
            "(message_id, account, mailbox, subject) "
            "VALUES (42, 'acc', 'INBOX', 'Test')"
        )
        rowid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO attachments "
            "(email_rowid, filename, mime_type, file_size, content_id) "
            "VALUES (?, 'doc.pdf', 'application/pdf', 500, NULL)",
            (rowid,),
        )
        conn.commit()

        result = manager.get_email_attachments(42)
        assert result is not None
        assert len(result) == 1
        assert result[0]["filename"] == "doc.pdf"
        assert result[0]["size"] == 500

    def test_not_found(self, temp_db_path):
        manager = IndexManager(db_path=temp_db_path)
        manager._get_conn()

        assert manager.get_email_attachments(999) is None


class TestGetStatsWithCapped:
    """Tests for capped_mailboxes in IndexStats (#17)."""

    def teardown_method(self):
        IndexManager._instance = None

    def test_get_stats_includes_capped_mailboxes(self, temp_db_path):
        manager = IndexManager(db_path=temp_db_path)
        conn = manager._get_conn()

        # Insert emails to hit the cap (default 5000)
        # Use a smaller cap via env override
        for i in range(3):
            conn.execute(
                "INSERT INTO emails (message_id, account, mailbox) "
                f"VALUES ({i}, 'acc', 'INBOX')"
            )
        conn.commit()

        with patch(
            "apple_mail_mcp.index.manager.get_index_max_emails",
            return_value=3,
        ):
            stats = manager.get_stats()

        assert stats.capped_mailboxes == 1

    def test_no_capped_mailboxes(self, temp_db_path):
        manager = IndexManager(db_path=temp_db_path)
        conn = manager._get_conn()
        conn.execute(
            "INSERT INTO emails (message_id, account, mailbox) "
            "VALUES (1, 'acc', 'INBOX')"
        )
        conn.commit()

        stats = manager.get_stats()
        assert stats.capped_mailboxes == 0


class TestDiskCountCache:
    """Tests for the get_stats() disk inventory TTL cache (#78)."""

    def teardown_method(self):
        IndexManager._instance = None

    def test_disk_count_cached_across_calls(self, temp_db_path, monkeypatch):
        manager = IndexManager(db_path=temp_db_path)

        call_count = {"n": 0}

        def fake_inventory(mail_dir, exclude_account_uuids=None):
            call_count["n"] += 1
            return {("acc", "INBOX", i): "/p" for i in range(7)}

        monkeypatch.setattr(
            "apple_mail_mcp.index.disk.find_mail_directory",
            lambda: "/fake",
        )
        monkeypatch.setattr(
            "apple_mail_mcp.index.disk.get_disk_inventory",
            fake_inventory,
        )

        # First call walks; second call returns cached value.
        s1 = manager.get_stats()
        s2 = manager.get_stats()
        assert s1.disk_email_count == 7
        assert s2.disk_email_count == 7
        assert call_count["n"] == 1, "second get_stats should hit cache"

    def test_disk_count_cache_expires(self, temp_db_path, monkeypatch):
        manager = IndexManager(db_path=temp_db_path)
        manager._DISK_COUNT_TTL_SEC = 0.05

        call_count = {"n": 0}

        def fake_inventory(mail_dir, exclude_account_uuids=None):
            call_count["n"] += 1
            return {("acc", "INBOX", i): "/p" for i in range(3)}

        monkeypatch.setattr(
            "apple_mail_mcp.index.disk.find_mail_directory",
            lambda: "/fake",
        )
        monkeypatch.setattr(
            "apple_mail_mcp.index.disk.get_disk_inventory",
            fake_inventory,
        )

        manager.get_stats()
        assert call_count["n"] == 1
        time.sleep(0.06)
        manager.get_stats()
        assert call_count["n"] == 2, "cache should re-fetch after TTL expiry"

    def test_disk_count_failure_not_cached(self, temp_db_path, monkeypatch):
        # Permission errors must not be cached — the next call should
        # retry in case Full Disk Access has since been granted.
        manager = IndexManager(db_path=temp_db_path)

        call_count = {"n": 0}

        def boom(_, exclude_account_uuids=None):
            call_count["n"] += 1
            raise PermissionError("no FDA")

        monkeypatch.setattr(
            "apple_mail_mcp.index.disk.find_mail_directory",
            lambda: "/fake",
        )
        monkeypatch.setattr(
            "apple_mail_mcp.index.disk.get_disk_inventory",
            boom,
        )

        s1 = manager.get_stats()
        s2 = manager.get_stats()
        assert s1.disk_email_count is None
        assert s2.disk_email_count is None
        assert call_count["n"] == 2, "failures must not be cached"

    def test_invalidate_disk_count_cache(self, temp_db_path, monkeypatch):
        manager = IndexManager(db_path=temp_db_path)

        call_count = {"n": 0}

        def fake_inventory(_, exclude_account_uuids=None):
            call_count["n"] += 1
            return {("acc", "INBOX", i): "/p" for i in range(5)}

        monkeypatch.setattr(
            "apple_mail_mcp.index.disk.find_mail_directory",
            lambda: "/fake",
        )
        monkeypatch.setattr(
            "apple_mail_mcp.index.disk.get_disk_inventory",
            fake_inventory,
        )

        manager.get_stats()
        manager.invalidate_disk_count_cache()
        manager.get_stats()
        assert call_count["n"] == 2, "invalidate should force a re-walk"


class TestWatcher:
    """Tests for file watcher integration."""

    def teardown_method(self):
        IndexManager._instance = None

    def test_watcher_not_running_initially_and_stop_is_safe(self, temp_db_path):
        """Watcher is not running initially; stop_watcher is a no-op."""
        manager = IndexManager(db_path=temp_db_path)
        assert manager.watcher_running is False
        manager.stop_watcher()  # Should not raise
        assert manager.watcher_running is False


class TestPerThreadConnections:
    """A long write must not block reads — the cause of a hung server."""

    def test_each_thread_gets_its_own_connection(self, temp_db_path):
        manager = IndexManager(db_path=temp_db_path)
        main_conn = manager._get_conn()
        other: list = []

        def worker() -> None:
            other.append(manager._get_conn())

        t = threading.Thread(target=worker)
        t.start()
        t.join()

        assert other[0] is not main_conn
        assert len(manager._open_conns) == 2

    def test_same_thread_reuses_its_connection(self, temp_db_path):
        manager = IndexManager(db_path=temp_db_path)
        assert manager._get_conn() is manager._get_conn()

    def test_read_is_not_blocked_by_an_open_write(self, temp_db_path):
        """The regression: a reader must answer while a writer holds a
        transaction open, instead of waiting for it to finish."""

        manager = IndexManager(db_path=temp_db_path)
        manager._get_conn()  # create schema first

        writing = threading.Event()
        release = threading.Event()
        failed: list = []

        def writer() -> None:
            conn = manager._get_conn()
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "INSERT INTO emails (message_id, account, mailbox) "
                    "VALUES (1, 'a', 'INBOX')"
                )
                writing.set()
                release.wait(timeout=10)
                conn.commit()
            except Exception as e:  # pragma: no cover - diagnostic
                failed.append(e)
                writing.set()

        t = threading.Thread(target=writer)
        t.start()
        assert writing.wait(timeout=10)

        # Reader on another connection, while the write txn is open.
        try:
            count = manager.indexed_email_count()
        finally:
            release.set()
            t.join()

        assert failed == []
        # Sees the pre-write snapshot rather than blocking on the writer.
        assert count == 0


class TestRebuildClearsCheaply:
    """Clearing must not fire the FTS trigger once per row."""

    def test_triggers_are_dropped_before_the_delete(self):
        """Regression: with emails_ad attached, DELETE FROM emails costs
        one FTS5 delete per row — minutes on a large index, and wasted,
        since the FTS content is rebuilt at the end."""
        import inspect

        from apple_mail_mcp.index.manager import IndexManager

        src = inspect.getsource(IndexManager.build_from_disk)
        drop_ad = src.index("DROP TRIGGER IF EXISTS emails_ad")
        delete_emails = src.index('conn.execute("DELETE FROM emails")')
        assert drop_ad < delete_emails, (
            "FTS triggers must be dropped before rows are deleted"
        )

    def test_fts_is_emptied_in_one_statement(self):
        import inspect

        from apple_mail_mcp.index.manager import IndexManager

        src = inspect.getsource(IndexManager.build_from_disk)
        assert "VALUES('delete-all')" in src

    def test_clearing_a_populated_index_is_fast(self, temp_db_path):
        """End-to-end: emptying a populated index must not crawl."""
        import time as _time

        from apple_mail_mcp.index.manager import IndexManager
        from apple_mail_mcp.index.schema import INSERT_EMAIL_SQL, email_to_row

        m = IndexManager(db_path=temp_db_path)
        conn = m._get_conn()
        for i in range(2000):
            conn.execute(
                INSERT_EMAIL_SQL,
                email_to_row(
                    {
                        "id": i,
                        "subject": f"subject {i}",
                        "content": "body " * 50,
                        "message_id_header": f"<{i}@x>",
                    },
                    "acct",
                    "INBOX",
                ),
            )
        conn.commit()

        # Same order the rebuild uses: triggers first, then delete.
        start = _time.monotonic()
        conn.execute("DROP TRIGGER IF EXISTS emails_ai")
        conn.execute("DROP TRIGGER IF EXISTS emails_ad")
        conn.execute("DROP TRIGGER IF EXISTS emails_au")
        conn.execute("DELETE FROM emails")
        conn.execute("INSERT INTO emails_fts(emails_fts) VALUES('delete-all')")
        conn.commit()
        elapsed = _time.monotonic() - start

        assert m.indexed_email_count() == 0
        assert elapsed < 2.0, f"clearing took {elapsed:.1f}s"


class TestBuildSyncMutualExclusion:
    """A build and a sync must never run against the DB at once."""

    def test_second_build_is_refused_while_one_holds_the_lock(
        self, temp_db_path
    ):
        from apple_mail_mcp.index.manager import IndexManager

        m = IndexManager(db_path=temp_db_path)
        assert m._write_lock.acquire(blocking=False)
        try:
            from apple_mail_mcp.index.manager import IndexBusyError

            with pytest.raises(IndexBusyError, match="already running"):
                m.build_from_disk()
        finally:
            m._write_lock.release()

    def test_sync_signals_busy_rather_than_faking_success(self, temp_db_path):
        """Returning 0 was indistinguishable from "no changes", so the
        tool reported a sync that never ran as up to date."""
        from apple_mail_mcp.index.manager import IndexBusyError, IndexManager

        m = IndexManager(db_path=temp_db_path)
        assert m._write_lock.acquire(blocking=False)
        try:
            with pytest.raises(IndexBusyError):
                m.sync_updates()
        finally:
            m._write_lock.release()

    def test_lock_is_released_when_the_build_fails(self, temp_db_path):
        """A failure used to leave _building stuck True forever, so the
        server reported 'building' until restart."""
        from unittest.mock import patch

        from apple_mail_mcp.index.manager import IndexManager

        m = IndexManager(db_path=temp_db_path)
        with patch(
            "apple_mail_mcp.index.disk.find_mail_directory",
            side_effect=PermissionError("no FDA"),
        ):
            with pytest.raises(PermissionError):
                m.build_from_disk()

        assert m.is_building() is False
        assert m.build_progress() is None
        assert m._write_lock.acquire(blocking=False)
        m._write_lock.release()

    def test_failed_build_leaves_the_fts_triggers_intact(
        self, temp_db_path, tmp_path
    ):
        """Triggers are dropped before the tables are cleared; if that
        window is unprotected, a failure permanently stops new mail from
        entering the search index."""
        from unittest.mock import patch

        from apple_mail_mcp.index.manager import IndexManager

        m = IndexManager(db_path=temp_db_path)
        conn = m._get_conn()

        def triggers() -> set:
            return {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='trigger'"
                )
            }

        assert {"emails_ai", "emails_ad", "emails_au"} <= triggers()

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

        assert {"emails_ai", "emails_ad", "emails_au"} <= triggers()


class TestBuildFinallyIsRobust:
    """The cleanup path must restore schema state come what may."""

    def test_triggers_restored_even_when_the_final_flush_fails(
        self, temp_db_path, tmp_path
    ):
        """The earlier test only failed before anything was batched, so
        it never exercised the flush path. A flush error must not skip
        trigger restoration — DROP TRIGGER autocommits, so the triggers
        would be gone from the file permanently."""
        from unittest.mock import patch

        from apple_mail_mcp.index.manager import IndexManager

        m = IndexManager(db_path=temp_db_path)
        conn = m._get_conn()

        def triggers() -> set:
            return {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='trigger'"
                )
            }

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
            for i in range(3)
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
                IndexManager,
                "_flush_batch",
                side_effect=sqlite3.OperationalError("database is locked"),
            ),
        ):
            # The flush failure is contained, not propagated...
            m.build_from_disk()

        # ...and the schema is intact.
        assert {"emails_ai", "emails_ad", "emails_au"} <= triggers()
        assert m.is_building() is False
        assert m._write_lock.acquire(blocking=False)
        m._write_lock.release()

    def test_lock_is_held_until_the_last_write(self):
        """Releasing before the flush/FTS rebuild let a waiting sync
        grab the lock and make those writes fail."""
        import inspect

        from apple_mail_mcp.index.manager import IndexManager

        src = inspect.getsource(IndexManager.build_from_disk)
        release = src.rindex("_write_lock.release()")
        for later in ("_recreate_fts_triggers", "rebuild_fts_index"):
            assert src.index(later) < release, later

    def test_triggers_are_restored_before_any_other_cleanup(self):
        import inspect

        from apple_mail_mcp.index.manager import IndexManager

        src = inspect.getsource(IndexManager.build_from_disk)
        # Anchored on the start of the cleanup rather than on the last
        # `finally:` — the lock release has its own nested one now, and
        # the claim is about the ORDER inside the cleanup.
        # Anchored on the start of the cleanup. `_building` is now
        # cleared at the END of it (a status call during the final flush
        # must not read "ready"), so the phase marker is what opens the
        # block.
        tail = src[src.index('self._mark_progress("finalizing")'):]
        assert tail.index("_recreate_fts_triggers") < tail.index("_flush_batch")


class TestWriteLockIsCrossProcess:
    """Claude Desktop starts a second server instance (upstream #106),
    so a threading.Lock cannot prevent "database is locked"."""

    def test_second_process_cannot_take_the_lock(self, tmp_path):
        import subprocess
        import sys
        import textwrap

        from apple_mail_mcp.index.manager import WriteLock

        lock_path = tmp_path / "index.lock"
        held = WriteLock(lock_path)
        assert held.acquire()
        try:
            probe = textwrap.dedent(f"""
                import sys
                sys.path.insert(0, {str(Path("src").resolve())!r})
                from pathlib import Path
                from apple_mail_mcp.index.manager import WriteLock
                got = WriteLock(Path({str(lock_path)!r})).acquire()
                print("ACQUIRED" if got else "REFUSED")
            """)
            out = subprocess.run(
                [sys.executable, "-c", probe],
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert "REFUSED" in out.stdout, (out.stdout, out.stderr)
        finally:
            held.release()

    def test_lock_is_available_again_after_release(self, tmp_path):
        import subprocess
        import sys
        import textwrap

        from apple_mail_mcp.index.manager import WriteLock

        lock_path = tmp_path / "index.lock"
        lock = WriteLock(lock_path)
        assert lock.acquire()
        lock.release()

        probe = textwrap.dedent(f"""
            import sys
            sys.path.insert(0, {str(Path("src").resolve())!r})
            from pathlib import Path
            from apple_mail_mcp.index.manager import WriteLock
            print("ACQUIRED" if WriteLock(Path({str(lock_path)!r})).acquire()
                  else "REFUSED")
        """)
        out = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert "ACQUIRED" in out.stdout, (out.stdout, out.stderr)

    def test_a_dead_holder_does_not_wedge_the_index(self, tmp_path):
        """The OS drops flock when a process dies — a crashed instance
        must not lock the index out permanently."""
        import subprocess
        import sys
        import textwrap

        from apple_mail_mcp.index.manager import WriteLock

        lock_path = tmp_path / "index.lock"
        grabber = textwrap.dedent(f"""
            import sys
            sys.path.insert(0, {str(Path("src").resolve())!r})
            from pathlib import Path
            from apple_mail_mcp.index.manager import WriteLock
            WriteLock(Path({str(lock_path)!r})).acquire()
            # exit without releasing
        """)
        subprocess.run(
            [sys.executable, "-c", grabber],
            capture_output=True,
            text=True,
            timeout=30,
        )

        lock = WriteLock(lock_path)
        assert lock.acquire(), "dead process still holds the lock"
        lock.release()

    def test_threads_in_one_process_are_still_serialized(self, tmp_path):
        """flock is per-file-description, so it would NOT stop a second
        thread here — the thread lock must still do that."""
        from apple_mail_mcp.index.manager import WriteLock

        lock = WriteLock(tmp_path / "index.lock")
        assert lock.acquire()
        try:
            assert lock.acquire() is False
        finally:
            lock.release()

    def test_manager_lock_lives_next_to_the_database(self, temp_db_path):
        from apple_mail_mcp.index.manager import IndexManager

        m = IndexManager(db_path=temp_db_path)
        assert m._write_lock._path == temp_db_path.with_suffix(".lock")


class TestFailedSyncDoesNotPoisonTheConnection:
    """sync_from_disk commits once, at the end, and never rolls back.
    A failure mid-way left an open write transaction that blocked every
    later write in the process with "database is locked"."""

    def test_write_still_possible_after_a_failed_sync(
        self, temp_db_path, tmp_path
    ):
        from unittest.mock import patch

        from apple_mail_mcp.index import sync as sync_mod
        from apple_mail_mcp.index.manager import IndexManager

        m = IndexManager(db_path=temp_db_path)
        conn = m._get_conn()

        def poison(conn_, *a, **k):
            # Open a write transaction, then die — exactly what a
            # mid-way failure in sync_from_disk does.
            conn_.execute(
                "INSERT INTO emails (message_id, account, mailbox) "
                "VALUES (999, 'a', 'INBOX')"
            )
            raise RuntimeError("sync exploded")

        with (
            patch(
                "apple_mail_mcp.index.disk.find_mail_directory",
                return_value=tmp_path,
            ),
            patch.object(sync_mod, "sync_from_disk", poison),
            pytest.raises(RuntimeError),
        ):
            m.sync_updates()

        # The poisoned row must be gone and the DB writable again.
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM emails WHERE message_id = 999"
            ).fetchone()[0]
            == 0
        )
        conn.execute(
            "INSERT INTO emails (message_id, account, mailbox) "
            "VALUES (1, 'a', 'INBOX')"
        )
        conn.commit()
        assert m.indexed_email_count() == 1


class TestUsableIndexAndErrorTracking:
    """has_usable_index() and last_error on the real IndexManager."""

    def test_empty_db_is_not_usable(self, temp_db_path):
        from apple_mail_mcp.index import IndexManager

        m = IndexManager(db_path=temp_db_path)
        # Create the file deliberately. indexed_email_count() no longer
        # does it as a side effect: a status call must not change the
        # state it describes.
        m._get_conn()
        assert m.has_index() is True
        assert m.has_usable_index() is False  # file exists, but no rows

    def test_sync_failure_sets_last_error_and_returns_zero(self, temp_db_path):
        from apple_mail_mcp.index import IndexManager

        m = IndexManager(db_path=temp_db_path)
        with patch(
            "apple_mail_mcp.index.disk.find_mail_directory",
            side_effect=PermissionError("Cannot access"),
        ):
            assert m.sync_updates() == 0  # contract preserved
        assert "PermissionError" in (m.last_error or "")


class TestBuildProgressVisibility:
    """During a build the user needs a percentage, not silence."""

    @pytest.mark.asyncio
    async def test_progress_reported_from_cached_disk_count(self, tmp_path):
        mgr = MagicMock()
        mgr.is_building.return_value = True
        mgr.write_lock_held.return_value = False
        mgr.has_index.return_value = True
        mgr.indexed_email_count.return_value = 17_500
        mgr.cached_disk_count.return_value = 70_000
        mgr.build_progress.return_value = {
            "phase": "indexing",
            "emails_done": 17_500,
            "files_seen": 17500,
            "seconds_since_progress": 2.0,
            "appears_stalled": False,
        }
        mgr.last_error = None

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
        assert r["disk_emails"] == 70_000
        assert r["progress_percent"] == 25.0
        # The expensive fresh walk must not run during a build.
        mgr.get_stats.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_cached_total_still_reports_count(self, tmp_path):
        mgr = MagicMock()
        mgr.is_building.return_value = True
        mgr.write_lock_held.return_value = False
        mgr.has_index.return_value = True
        mgr.indexed_email_count.return_value = 42
        mgr.cached_disk_count.return_value = None  # never walked yet
        mgr.build_progress.return_value = {
            "phase": "indexing",
            "emails_done": 42,
            "files_seen": 42,
            "seconds_since_progress": 1.0,
            "appears_stalled": False,
        }
        mgr.last_error = None

        with (
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch(
                "apple_mail_mcp.index.disk.find_mail_directory",
                return_value=tmp_path,
            ),
        ):
            from apple_mail_mcp.server import get_index_status

            r = await get_index_status()

        assert r["indexed_emails"] == 42
        assert "progress_percent" not in r  # honest: no denominator


class TestStallDetection:
    """ "Working" and "wedged" must be distinguishable."""

    def _building_mgr(self, seconds_ago, phase="indexing"):
        m = MagicMock()
        m.is_building.return_value = True
        m.write_lock_held.return_value = False
        m.has_index.return_value = True
        m.indexed_email_count.return_value = 0
        m.cached_disk_count.return_value = 70_000
        m.build_progress.return_value = {
            "phase": phase,
            "emails_done": 500,
            "files_seen": 500,
            "seconds_since_progress": seconds_ago,
            "appears_stalled": seconds_ago > 120,
        }
        m.last_error = None
        return m

    async def _status(self, mgr, tmp_path):
        with (
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch(
                "apple_mail_mcp.index.disk.find_mail_directory",
                return_value=tmp_path,
            ),
        ):
            from apple_mail_mcp.server import get_index_status

            return await get_index_status()

    @pytest.mark.asyncio
    async def test_recent_progress_is_not_a_problem(self, tmp_path):
        r = await self._status(self._building_mgr(3.0), tmp_path)
        assert r["build_appears_stalled"] is False
        assert r["seconds_since_progress"] == 3.0
        assert "problem" not in r

    @pytest.mark.asyncio
    async def test_long_silence_is_reported_as_stalled(self, tmp_path):
        r = await self._status(self._building_mgr(600.0), tmp_path)
        assert r["build_appears_stalled"] is True
        assert "stuck" in r["problem"].lower()
        assert any("Cmd-Q" in s for s in r["next_steps"])

    @pytest.mark.asyncio
    async def test_reports_rows_written_even_when_count_reads_zero(
        self, tmp_path
    ):
        """The committed-batch counter proves work happened, even if the
        table count is still catching up."""
        r = await self._status(self._building_mgr(5.0), tmp_path)
        assert r["build_emails_done"] == 500


class TestWarmUpPhaseIsNotMistakenForAStall:
    """Zero indexed during metadata reading is expected, not a fault."""

    def _mgr(self, phase, idle):
        m = MagicMock()
        m.is_building.return_value = True
        m.write_lock_held.return_value = False
        m.has_index.return_value = True
        m.indexed_email_count.return_value = 0
        m.cached_disk_count.return_value = 63_953
        m.build_progress.return_value = {
            "phase": phase,
            "emails_done": 0,
            "files_seen": 0,
            "seconds_since_progress": idle,
            "appears_stalled": False,
        }
        m.last_error = None
        return m

    async def _status(self, mgr, tmp_path):
        with (
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch(
                "apple_mail_mcp.index.disk.find_mail_directory",
                return_value=tmp_path,
            ),
        ):
            from apple_mail_mcp.server import get_index_status

            return await get_index_status()

    @pytest.mark.asyncio
    async def test_metadata_phase_explains_the_zero(self, tmp_path):
        r = await self._status(self._mgr("reading_metadata", 150.0), tmp_path)

        assert r["build_phase"] == "reading_metadata"
        assert "problem" not in r  # 150s of no writes is normal here
        assert "warm-up" in r["user_message"]
        assert any("zero" in s.lower() for s in r["next_steps"])

    @pytest.mark.asyncio
    async def test_indexing_phase_with_zero_reads_differently(self, tmp_path):
        r = await self._status(self._mgr("indexing", 5.0), tmp_path)

        assert r["build_phase"] == "indexing"
        assert "warm-up" not in r["user_message"]

    def test_metadata_phase_gets_a_longer_grace_period(self, temp_db_path):
        """Reading metadata legitimately takes minutes; indexing must not."""
        from apple_mail_mcp.index import IndexManager

        m = IndexManager(db_path=temp_db_path)
        assert (
            m._STALL_SECONDS["reading_metadata"] > m._STALL_SECONDS["indexing"]
        )

    def test_heartbeat_reports_phase_and_liveness(self, temp_db_path):
        from apple_mail_mcp.index import IndexManager

        m = IndexManager(db_path=temp_db_path)
        assert m.build_progress() is None  # nothing running

        m._mark_progress("reading_metadata")
        p = m.build_progress()
        assert p["phase"] == "reading_metadata"
        assert p["emails_done"] == 0
        assert p["appears_stalled"] is False

        m._mark_progress("indexing", done=500, seen=512)
        p = m.build_progress()
        assert p["phase"] == "indexing"
        assert p["emails_done"] == 500
        assert p["files_seen"] == 512


class TestRunningSyncIsVisible:
    """A sync holds the write lock but sets no build phase, so it was
    invisible: counts frozen, last_sync stale, state 'ready'."""

    @pytest.mark.asyncio
    async def test_status_reports_a_running_sync(self, tmp_path):
        mgr = MagicMock()
        mgr.is_building.return_value = False
        mgr.write_lock_held.return_value = True
        mgr.has_index.return_value = True
        mgr.indexed_email_count.return_value = 63_875
        mgr.cached_disk_count.return_value = 63_977
        mgr.build_progress.return_value = None
        mgr.last_error = None

        with (
            patch("apple_mail_mcp.server._get_index_manager", return_value=mgr),
            patch(
                "apple_mail_mcp.index.disk.find_mail_directory",
                return_value=tmp_path,
            ),
        ):
            from apple_mail_mcp.server import get_index_status

            r = await get_index_status()

        assert r["sync_running"] is True
        assert "update is running" in r["user_message"]
        # Not a fault — no fresh disk walk, no "problem".
        assert "problem" not in r
        mgr.get_stats.assert_not_called()


class TestLockErrorIsExplainedCorrectly:
    """Twice in a live session the assistant blamed Apple Mail for
    "database is locked" and told the user to quit Mail. Mail.app never
    touches this database."""

    @pytest.mark.asyncio
    async def test_guidance_names_the_real_cause(self, tmp_path):
        mgr = MagicMock()
        mgr.is_building.return_value = False
        mgr.write_lock_held.return_value = False
        mgr.has_index.return_value = True
        mgr.indexed_email_count.return_value = 63_875
        mgr.count_skipped_too_large.return_value = 0
        mgr.count_without_stable_id.return_value = 0
        mgr.build_progress.return_value = None
        mgr.recent_events.return_value = []
        mgr.last_error = "OperationalError: database is locked"
        stats = MagicMock()
        stats.disk_email_count = 63_900
        stats.mailbox_count = 24
        stats.attachment_count = 0
        stats.db_size_mb = 485.0
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

        steps = " ".join(r["next_steps"]).lower()
        assert "not apple mail's" in steps
        assert "quitting mail does not help" in steps
        assert "two copies" in steps
        assert (
            "apple mail has nothing to do with it" in r["user_message"].lower()
        )


class TestAFailedFinalizationStillEndsTheBuild:
    """The heaviest writes in the program run in the build's `finally`.
    A failure in any of them skipped the two lines that end the build,
    so `is_building()` stayed True for the life of the process and the
    status tool reported a build that had been dead for hours."""

    def test_an_fts_rebuild_failure_does_not_leave_a_phantom_build(
        self, temp_db_path, tmp_path
    ):
        from unittest.mock import patch

        from apple_mail_mcp.index import manager as manager_mod
        from apple_mail_mcp.index.manager import IndexManager

        manager = IndexManager(db_path=temp_db_path)

        with (
            patch(
                "apple_mail_mcp.index.disk.find_mail_directory",
                return_value=tmp_path,
            ),
            patch.object(manager, "_resolve_exclusions", return_value=set()),
            patch(
                "apple_mail_mcp.index.disk.scan_all_emails",
                return_value=iter(
                    [
                        {
                            "id": 1,
                            "account": "uuid-1",
                            "mailbox": "INBOX",
                            "subject": "hello",
                        }
                    ]
                ),
            ),
            # The last write of the build, and the one most likely to
            # fail on a damaged index.
            patch.object(
                manager_mod,
                "rebuild_fts_index",
                side_effect=RuntimeError("fts is corrupt"),
            ),
            pytest.raises(RuntimeError),
        ):
            manager.build_from_disk()

        assert not manager.is_building(), (
            "a failed finalization left the build reported as running"
        )
        assert manager.last_error, (
            "the finalization failure was not recorded anywhere"
        )


class TestAnInterruptRollsBackToo:
    """Ctrl-C lands mid-sync as easily as a bug does, and leaves exactly
    the same open transaction — which then blocks every later write.
    `except Exception` does not catch it."""

    def test_an_interrupt_also_rolls_back(self, temp_db_path):
        from unittest.mock import MagicMock, patch

        from apple_mail_mcp.index.manager import IndexManager

        mgr = IndexManager(db_path=temp_db_path)
        conn = MagicMock()
        with (
            patch.object(mgr, "_get_conn", return_value=conn),
            patch.object(mgr, "_resolve_exclusions", return_value=set()),
            patch(
                "apple_mail_mcp.index.disk.find_mail_directory",
                return_value=temp_db_path.parent,
            ),
            patch(
                "apple_mail_mcp.index.sync.sync_from_disk",
                side_effect=KeyboardInterrupt(),
            ),
            pytest.raises(KeyboardInterrupt),
        ):
            mgr.sync_updates()

        conn.rollback.assert_called_once()


class TestASyncThatFailsBeforeItStartsIsStillRecorded:
    """`last_error` is cleared at the top of the sync. Anything that
    fails after that but outside the try — resolving the account
    exclusions talks to Mail, opening the database runs migrations —
    raised while the status tool reported no error at all."""

    def test_a_failure_resolving_exclusions_is_recorded(
        self, temp_db_path, tmp_path
    ):
        from unittest.mock import patch

        from apple_mail_mcp.index.manager import IndexManager

        mgr = IndexManager(db_path=temp_db_path)
        with (
            patch(
                "apple_mail_mcp.index.disk.find_mail_directory",
                return_value=tmp_path,
            ),
            patch.object(
                mgr,
                "_resolve_exclusions",
                side_effect=RuntimeError("Mail refused the Apple Event"),
            ),
            pytest.raises(RuntimeError),
        ):
            mgr.sync_updates()

        assert mgr.last_error, "the sync failed and recorded nothing"
        assert "Apple Event" in mgr.last_error


class TestBatchedStableIdLookup:
    """`get_rfc822_ids()` — one query for a whole page of results."""

    def _insert(self, m, mid, account, mailbox, header):
        from apple_mail_mcp.index.schema import (
            INSERT_EMAIL_SQL,
            email_to_row,
        )

        conn = m._get_conn()
        conn.execute(
            INSERT_EMAIL_SQL,
            email_to_row(
                {"id": mid, "subject": "s", "message_id_header": header},
                account,
                mailbox,
                f"/tmp/{mid}.emlx",
            ),
        )
        conn.commit()

    def test_returns_the_header_for_known_rows(self, temp_db_path):
        m = IndexManager(db_path=temp_db_path)
        self._insert(m, 1, "acct", "INBOX", "<one@x>")
        self._insert(m, 2, "acct", "INBOX", "<two@x>")

        out = m.get_rfc822_ids([("acct", "INBOX", 1), ("acct", "INBOX", 2)])
        assert out == {
            ("acct", "INBOX", 1): "<one@x>",
            ("acct", "INBOX", 2): "<two@x>",
        }

    def test_the_lookup_is_scoped_to_the_mailbox(self, temp_db_path):
        """A Mail.app id is unique only within its mailbox: the same
        number in another mailbox is a different message."""
        m = IndexManager(db_path=temp_db_path)
        self._insert(m, 1, "acct", "INBOX", "<inbox@x>")
        self._insert(m, 1, "acct", "Sent", "<sent@x>")

        assert m.get_rfc822_ids([("acct", "Sent", 1)]) == {
            ("acct", "Sent", 1): "<sent@x>"
        }

    def test_rows_without_a_header_are_absent(self, temp_db_path):
        """Rows indexed before v6 have NULL. Absent means "unknown" —
        never an empty string that could match something."""
        m = IndexManager(db_path=temp_db_path)
        self._insert(m, 1, "acct", "INBOX", "")

        assert m.get_rfc822_ids([("acct", "INBOX", 1)]) == {}

    def test_empty_input_asks_nothing(self, temp_db_path):
        m = IndexManager(db_path=temp_db_path)
        assert m.get_rfc822_ids([]) == {}

    def test_a_large_page_stays_under_the_variable_limit(self, temp_db_path):
        """Three variables per key; SQLite's default cap is 999."""
        m = IndexManager(db_path=temp_db_path)
        for i in range(400):
            self._insert(m, i, "acct", "INBOX", f"<m{i}@x>")

        out = m.get_rfc822_ids([("acct", "INBOX", i) for i in range(400)])
        assert len(out) == 400


class TestBuildOnlySignalsStartedAfterItCanActuallyRun:
    """`on_started` is what the rebuild tool reports "started" on. Firing
    it before the steps that can still refuse the build — talking to
    Mail for the account exclusions, opening and initialising SQLite —
    made "started" survive both failures."""

    def test_a_build_that_cannot_open_the_database_never_signals(
        self, tmp_path
    ):
        import sqlite3
        from unittest.mock import patch

        from apple_mail_mcp.index.manager import IndexManager

        manager = IndexManager(db_path=tmp_path / "index.db")
        signalled = []

        with (
            patch.object(
                manager,
                "_get_conn",
                side_effect=sqlite3.OperationalError("unable to open"),
            ),
            patch.object(manager, "_resolve_exclusions", return_value=set()),
            patch(
                "apple_mail_mcp.index.disk.find_mail_directory",
                return_value=tmp_path,
            ),
        ):
            with pytest.raises(sqlite3.OperationalError):
                manager.build_from_disk(on_started=lambda: signalled.append(1))

        assert signalled == [], (
            "a build that never opened the database reported that it started"
        )


class TestBuildProgressHeartbeat:
    """A build runs on a daemon thread. Without a heartbeat, "working"
    and "wedged" are the same observation from outside: zero indexed."""

    def test_nothing_running_has_no_progress(self, temp_db_path):
        m = IndexManager(db_path=temp_db_path)
        assert m.build_progress() is None
        assert m.is_building() is False

    def test_heartbeat_reports_phase_and_liveness(self, temp_db_path):
        m = IndexManager(db_path=temp_db_path)

        m._mark_progress("reading_metadata")
        p = m.build_progress()
        assert p["phase"] == "reading_metadata"
        assert p["emails_done"] == 0
        assert p["appears_stalled"] is False

        m._mark_progress("indexing", done=500, seen=512)
        p = m.build_progress()
        assert p["phase"] == "indexing"
        assert p["emails_done"] == 500
        assert p["files_seen"] == 512

    def test_metadata_phase_gets_a_longer_grace_period(self, temp_db_path):
        """Reading Apple's metadata for a large mailbox legitimately
        takes minutes with nothing written; the indexing loop must tick
        every few seconds."""
        m = IndexManager(db_path=temp_db_path)
        assert (
            m._STALL_SECONDS["reading_metadata"] > m._STALL_SECONDS["indexing"]
        )

    def test_silence_beyond_the_phase_budget_reads_as_stalled(
        self, temp_db_path
    ):
        m = IndexManager(db_path=temp_db_path)
        m._mark_progress("indexing", done=500, seen=500)
        # Backdate the stamp instead of sleeping.
        m._build_progress["ts"] -= m._STALL_SECONDS["indexing"] + 1
        assert m.build_progress()["appears_stalled"] is True


class TestBuildStateSurvivesFailure:
    """A build that dies must not leave a phantom build on display."""

    def test_failed_build_clears_the_flag_and_records_the_error(
        self, temp_db_path
    ):
        m = IndexManager(db_path=temp_db_path)
        with (
            patch(
                "apple_mail_mcp.index.disk.find_mail_directory",
                side_effect=PermissionError("no Full Disk Access"),
            ),
            pytest.raises(PermissionError),
        ):
            m.build_from_disk()

        assert m.is_building() is False
        assert m.build_progress() is None
        assert "PermissionError" in (m.last_error or "")
        assert any(
            e["message"] == "Index build failed" for e in m.recent_events()
        )

    def test_failure_before_the_db_opens_does_not_mask_itself(
        self, temp_db_path
    ):
        """The cleanup path writes through `conn`, which does not exist
        when the mail directory could not even be found. Raising
        UnboundLocalError from `finally` would hide the real cause."""
        m = IndexManager(db_path=temp_db_path)
        with (
            patch(
                "apple_mail_mcp.index.disk.find_mail_directory",
                side_effect=FileNotFoundError("no ~/Library/Mail"),
            ),
            pytest.raises(FileNotFoundError),
        ):
            m.build_from_disk()


class TestConnectionsArePerThread:
    """A shared connection made a background rebuild block the server.

    SQLite connections are not safe to share across threads, and the
    single instance-level one meant every request needing the index
    waited behind a rebuild — from outside, the server simply looked
    frozen.
    """

    def test_each_thread_gets_its_own(self, temp_db_path):
        import threading

        from apple_mail_mcp.index.manager import IndexManager

        mgr = IndexManager(db_path=temp_db_path)
        seen = []

        def grab():
            seen.append(id(mgr._get_conn()))

        main = id(mgr._get_conn())
        t = threading.Thread(target=grab)
        t.start()
        t.join()

        assert seen and seen[0] != main

    def test_the_same_thread_keeps_its_connection(self, temp_db_path):
        from apple_mail_mcp.index.manager import IndexManager

        mgr = IndexManager(db_path=temp_db_path)
        assert mgr._get_conn() is mgr._get_conn()

    def test_close_releases_every_thread_connection(self, temp_db_path):
        import threading

        from apple_mail_mcp.index.manager import IndexManager

        mgr = IndexManager(db_path=temp_db_path)
        mgr._get_conn()
        t = threading.Thread(target=mgr._get_conn)
        t.start()
        t.join()

        assert len(mgr._open_conns) == 2
        mgr.close()
        assert not mgr._open_conns


class TestConnectionsDoNotOutliveTheirThreads:
    """One connection per thread is right; keeping every one of them
    alive until close() is not. A server that runs short-lived worker
    threads accumulated a SQLite file descriptor — and an open implicit
    transaction — per thread that had ever touched the index."""

    def test_a_finished_thread_s_connection_is_closed(self, temp_db_path):
        import sqlite3
        import threading

        from apple_mail_mcp.index.manager import IndexManager

        mgr = IndexManager(db_path=temp_db_path)
        mgr._get_conn()  # the main thread's own connection

        captured: list[sqlite3.Connection] = []
        worker = threading.Thread(
            target=lambda: captured.append(mgr._get_conn())
        )
        worker.start()
        worker.join()

        # The reap runs on the next connection request.
        threading.Thread(target=mgr._get_conn).start()
        time.sleep(0.2)

        with pytest.raises(sqlite3.ProgrammingError):
            captured[0].execute("SELECT 1")


class TestEventRing:
    """An MCP server's stderr reaches nobody under a desktop client, so
    this ring is the only answer to "what just happened?"."""

    def test_events_are_newest_first(self, temp_db_path):
        m = IndexManager(db_path=temp_db_path)
        m.record_event("info", "first")
        m.record_event("info", "second")
        assert [e["message"] for e in m.recent_events()][:2] == [
            "second",
            "first",
        ]

    def test_ring_is_bounded(self, temp_db_path):
        from apple_mail_mcp.index.manager import MAX_EVENTS

        m = IndexManager(db_path=temp_db_path)
        for i in range(MAX_EVENTS + 25):
            m.record_event("info", f"event {i}")
        assert len(m.recent_events(limit=1000)) == MAX_EVENTS

    def test_fields_are_stringified_not_dropped(self, temp_db_path):
        """These go into an MCP response: one non-serializable value
        would break the whole reply rather than this one event."""
        m = IndexManager(db_path=temp_db_path)
        m.record_event("info", "with fields", path=Path("/tmp/x"), count=3)
        e = m.recent_events()[0]
        assert e["path"] == "/tmp/x"
        assert e["count"] == "3"

    def test_recording_never_raises(self, temp_db_path):
        """Diagnostics must not be able to break what they describe."""

        class Hostile:
            def __str__(self):
                raise RuntimeError("nope")

        m = IndexManager(db_path=temp_db_path)
        m.record_event("info", "hostile", bad=Hostile())  # must not raise


class TestFailedSyncDoesNotWedgeTheIndex:
    """A sync that raises must not leave its transaction open.

    It did: every later write then failed with "database is locked"
    until the process restarted, and the index looked dead while
    nothing was actually wrong with it. Two other causes were chased
    first because the symptom is identical.
    """

    def test_rollback_runs_and_the_error_propagates(self, temp_db_path):
        from unittest.mock import MagicMock, patch

        from apple_mail_mcp.index.manager import IndexManager

        mgr = IndexManager(db_path=temp_db_path)
        conn = MagicMock()
        with (
            patch.object(mgr, "_get_conn", return_value=conn),
            patch.object(mgr, "_resolve_exclusions", return_value=set()),
            patch(
                "apple_mail_mcp.index.disk.find_mail_directory",
                return_value=temp_db_path.parent,
            ),
            patch(
                "apple_mail_mcp.index.sync.sync_from_disk",
                side_effect=RuntimeError("disk went away mid-run"),
            ),
            pytest.raises(RuntimeError),
        ):
            mgr.sync_updates()

        conn.rollback.assert_called_once()

    def test_a_failing_rollback_does_not_mask_the_real_error(
        self, temp_db_path
    ):
        from unittest.mock import MagicMock, patch

        from apple_mail_mcp.index.manager import IndexManager

        mgr = IndexManager(db_path=temp_db_path)
        conn = MagicMock()
        conn.rollback.side_effect = RuntimeError("rollback failed too")
        with (
            patch.object(mgr, "_get_conn", return_value=conn),
            patch.object(mgr, "_resolve_exclusions", return_value=set()),
            patch(
                "apple_mail_mcp.index.disk.find_mail_directory",
                return_value=temp_db_path.parent,
            ),
            patch(
                "apple_mail_mcp.index.sync.sync_from_disk",
                side_effect=RuntimeError("the original failure"),
            ),
            pytest.raises(RuntimeError, match="the original failure"),
        ):
            mgr.sync_updates()


class TestFinalizationFailuresAreRecorded:
    """ "Every failure" has to include the ones after the except block.

    The heaviest writes — the final flush and the FTS rebuild — happen in
    the cleanup block, past the `except`. A failure there escaped with
    `last_error` still None, so a build that left rows body search can
    never find reported itself as clean.
    """

    def test_a_failing_fts_rebuild_is_not_a_clean_build(self, tmp_path):
        from unittest.mock import patch

        from apple_mail_mcp.index import IndexManager

        mgr = IndexManager(db_path=tmp_path / "idx.db")

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
            patch(
                "apple_mail_mcp.index.manager.rebuild_fts_index",
                side_effect=sqlite3.OperationalError("disk I/O error"),
            ),
        ):
            mgr.build_from_disk()

        assert mgr.last_error is not None, (
            "a build whose FTS rebuild failed reported itself as clean"
        )
        assert "disk I/O error" in mgr.last_error
        assert any(e["level"] == "error" for e in mgr.recent_events())


class TestFindByRfc822:
    """Locating an indexed message by its stable header."""

    def _insert(self, m, mid, account, mailbox, header):
        from apple_mail_mcp.index.schema import (
            INSERT_EMAIL_SQL,
            email_to_row,
        )

        conn = m._get_conn()
        conn.execute(
            INSERT_EMAIL_SQL,
            email_to_row(
                {"id": mid, "subject": "s", "message_id_header": header},
                account,
                mailbox,
                f"/tmp/{mid}.emlx",
            ),
        )
        conn.commit()

    def test_either_stored_form_matches(self, temp_db_path):
        """The .emlx keeps the angle brackets, Apple Mail's messageId
        drops them. A strict comparison finds nothing."""
        m = IndexManager(db_path=temp_db_path)
        self._insert(m, 1, "acct", "INBOX", "<bracketed@x>")
        self._insert(m, 2, "acct", "INBOX", "bare@x")

        assert m.find_by_rfc822("bracketed@x")
        assert m.find_by_rfc822("<bracketed@x>")
        assert m.find_by_rfc822("bare@x")
        assert m.find_by_rfc822("<bare@x>")

    def test_a_folded_header_still_matches(self, temp_db_path):
        """trim() drops spaces but not tabs or newlines, and a folded
        header brings exactly those."""
        m = IndexManager(db_path=temp_db_path)
        self._insert(m, 1, "acct", "INBOX", "<\n\tfolded@x >")

        assert m.find_by_rfc822("folded@x")

    def test_every_copy_is_returned(self, temp_db_path):
        """The same mail legitimately exists in INBOX and an archive, or
        across accounts — guessing one would be wrong half the time."""
        m = IndexManager(db_path=temp_db_path)
        self._insert(m, 1, "acct", "INBOX", "<dup@x>")
        self._insert(m, 2, "acct", "Archive", "<dup@x>")

        assert len(m.find_by_rfc822("<dup@x>")) == 2

    def test_an_unknown_header_returns_nothing(self, temp_db_path):
        m = IndexManager(db_path=temp_db_path)
        assert m.find_by_rfc822("<nope@x>") == []


class TestFirstConnectionsDoNotRaceOnSchema:
    """One connection per thread means several threads can meet a fresh
    database at once — and `init_database()` creates tables and runs
    migrations. Measured before the lock was added: "vtable constructor
    failed: emails_fts", "duplicate column name: emlx_path", and
    spurious migration notices."""

    def test_twelve_threads_on_a_fresh_database(self, tmp_path):
        import concurrent.futures as cf

        from apple_mail_mcp.index import IndexManager

        mgr = IndexManager(db_path=tmp_path / "fresh.db")
        errors: list[str] = []

        def hit() -> None:
            try:
                mgr._get_conn().execute("SELECT 1").fetchone()
            except Exception as exc:  # noqa: BLE001 - reporting the race
                errors.append(f"{type(exc).__name__}: {exc}")

        with cf.ThreadPoolExecutor(max_workers=12) as pool:
            list(pool.map(lambda _: hit(), range(12)))

        assert not errors, errors
        mgr.close()

    def test_the_lock_is_only_taken_on_a_first_call(self, temp_db_path):
        """Serializing every query would undo the change this unit is
        about, so the thread-local has to short-circuit."""
        from apple_mail_mcp.index import IndexManager

        mgr = IndexManager(db_path=temp_db_path)
        first = mgr._get_conn()

        class Tripwire:
            def __enter__(self):
                raise AssertionError("lock taken on a repeat call")

            def __exit__(self, *a):
                return False

        mgr._conn_lock = Tripwire()
        assert mgr._get_conn() is first


class TestIndexWritesAreSerializedAcrossProcesses:
    """A thread lock is not enough here.

    Claude Desktop starts this server twice, so two processes contend
    for the same index file. Locking correctly inside each process
    still produced "database is locked" — the lock has to live in the
    filesystem.
    """

    def test_a_second_holder_is_refused(self, temp_db_path):
        from apple_mail_mcp.index.manager import WriteLock

        a = WriteLock(temp_db_path)
        b = WriteLock(temp_db_path)
        assert a.acquire(blocking=False)
        try:
            assert not b.acquire(blocking=False)
        finally:
            a.release()
        assert b.acquire(blocking=False)
        b.release()

    def test_a_real_second_process_is_refused(self, temp_db_path):
        """Two lock objects in one process share a file lock, so they
        cannot show that the lock crosses the process boundary — which
        is the entire claim. This one forks a real interpreter."""
        import subprocess
        import sys
        import textwrap

        from apple_mail_mcp.index.manager import WriteLock

        holder = subprocess.Popen(
            [
                sys.executable,
                "-c",
                textwrap.dedent(f"""
                    import sys, time
                    from apple_mail_mcp.index.manager import WriteLock
                    lock = WriteLock({str(temp_db_path)!r})
                    print("held" if lock.acquire(blocking=False) else "no",
                          flush=True)
                    time.sleep(30)
                """),
            ],
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            assert holder.stdout is not None
            assert holder.stdout.readline().strip() == "held"
            mine = WriteLock(temp_db_path)
            assert not mine.acquire(blocking=False), (
                "a second PROCESS took a lock the first one holds"
            )
        finally:
            holder.kill()
            holder.wait(timeout=10)

        mine = WriteLock(temp_db_path)
        assert mine.acquire(blocking=False), (
            "the lock outlived the process that held it"
        )
        mine.release()

    def test_a_lock_file_that_cannot_be_created_is_not_a_lock(
        self, temp_db_path, caplog
    ):
        """Returning True there reports an exclusivity nobody took.
        Refusing instead would wedge every write on a read-only home,
        so it degrades — but it has to be visible when it does."""
        import logging
        from unittest.mock import patch

        from apple_mail_mcp.index.manager import WriteLock

        lock = WriteLock(temp_db_path)
        with (
            patch("os.open", side_effect=PermissionError("read-only home")),
            caplog.at_level(logging.WARNING),
        ):
            assert lock.acquire(blocking=False)
        try:
            assert lock.degraded, (
                "the lock degraded to this process and did not say so"
            )
            assert "NOT prevented" in caplog.text
        finally:
            lock.release()

    def test_a_filesystem_without_flock_does_not_wedge_the_index(
        self, temp_db_path, caplog
    ):
        """ENOTSUP is not contention. Treating every OSError as "held"
        made the index permanently unwritable on the network home
        directories that answer it, with "already running" as the only
        explanation the user ever saw."""
        import errno
        import logging
        from unittest.mock import patch

        from apple_mail_mcp.index.manager import WriteLock

        lock = WriteLock(temp_db_path)
        with (
            patch(
                "fcntl.flock",
                side_effect=OSError(errno.ENOTSUP, "not supported"),
            ),
            caplog.at_level(logging.WARNING),
        ):
            assert lock.acquire(blocking=False)
        try:
            assert lock.degraded
        finally:
            lock.release()

    def test_a_lock_someone_else_holds_is_still_a_refusal(self, temp_db_path):
        """The errno split must not turn real contention into a pass."""
        from apple_mail_mcp.index.manager import WriteLock

        a = WriteLock(temp_db_path)
        b = WriteLock(temp_db_path)
        assert a.acquire(blocking=False)
        try:
            assert not b.acquire(blocking=False)
            assert not b.degraded
        finally:
            a.release()

    def test_release_lets_the_next_one_in(self, temp_db_path):
        from apple_mail_mcp.index.manager import WriteLock

        lock = WriteLock(temp_db_path)
        assert lock.acquire(blocking=False)
        lock.release()
        assert lock.acquire(blocking=False)
        lock.release()

    def test_a_busy_index_raises_rather_than_reporting_no_changes(
        self, temp_db_path
    ):
        """0 changes and "never ran" must not look the same."""
        from apple_mail_mcp.index.manager import IndexBusyError, IndexManager

        mgr = IndexManager(db_path=temp_db_path)
        assert mgr._write_lock.acquire(blocking=False)
        try:
            with pytest.raises(IndexBusyError):
                mgr.sync_updates()
            with pytest.raises(IndexBusyError):
                mgr.build_from_disk()
        finally:
            mgr._write_lock.release()

    def test_an_unusable_lock_file_degrades_to_thread_locking(
        self, temp_db_path
    ):
        """A read-only home must not make the index unusable.

        Patching `builtins.open` proved nothing: the lock uses `os.open`,
        so the fallback was never exercised and the test passed either
        way.
        """
        from unittest.mock import patch

        from apple_mail_mcp.index.manager import WriteLock

        with patch(
            "os.open", side_effect=OSError("read-only file system")
        ) as opened:
            lock = WriteLock(temp_db_path)
            assert lock.acquire(blocking=False)
            lock.release()

        assert opened.called, "the file-lock path was never reached"


class TestReadingTheStatusCreatesNothing:
    """`get_index_status()` is documented as read-only. Opening a
    connection runs init_database(), so counting rows on a fresh install
    created the index file — and the next `serve` then took the sync
    path instead of building."""

    def test_counting_rows_does_not_create_the_database(self, tmp_path):
        from apple_mail_mcp.index import IndexManager

        db = tmp_path / "absent.db"
        mgr = IndexManager(db_path=db)

        assert mgr.indexed_email_count() == 0
        assert not db.exists(), "a status read created the index"
        assert mgr.has_index() is False


class TestRebuildDoesNotFireOneDeletePerRow:
    """Clearing with the FTS triggers still in place is quadratic work.

    emails_ad fires once per deleted row against the external content
    index — minutes on a 64k-message mailbox, for a table that is about
    to be empty. Dropping the triggers first and emptying FTS with a
    single statement removes all of it.
    """

    def test_triggers_are_dropped_before_the_delete(self):
        import inspect

        from apple_mail_mcp.index import manager as m

        src = inspect.getsource(m.IndexManager.build_from_disk)
        drop = src.index("DROP TRIGGER IF EXISTS emails_ad")
        delete = src.index('conn.execute("DELETE FROM emails")')
        assert drop < delete, "the DELETE would fire the trigger per row"

    def test_fts_is_emptied_in_one_statement(self):
        import inspect

        from apple_mail_mcp.index import manager as m

        src = inspect.getsource(m.IndexManager.build_from_disk)
        assert "VALUES('delete-all')" in src

    def test_triggers_are_restored_even_when_the_build_fails(
        self, temp_db_path
    ):
        """DDL commits implicitly, so a rollback does not bring them
        back: without the restore the index silently stops tracking
        its FTS table from then on."""
        from unittest.mock import patch

        from apple_mail_mcp.index.manager import IndexManager

        mgr = IndexManager(db_path=temp_db_path)
        conn = mgr._get_conn()
        with (
            patch(
                "apple_mail_mcp.index.disk.find_mail_directory",
                return_value=temp_db_path.parent,
            ),
            patch(
                "apple_mail_mcp.index.disk.scan_all_emails",
                side_effect=RuntimeError("disk went away"),
            ),
        ):
            with pytest.raises(RuntimeError):
                mgr.build_from_disk()

        names = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            )
        }
        assert {"emails_ai", "emails_ad", "emails_au"} <= names


class TestSingleRowCleanupTakesTheWriteLockToo:
    """`delete_email()` is a write. Outside the lock it raced a build
    and raised "database is locked" into a read that was otherwise
    fine."""

    def test_a_delete_during_a_build_is_skipped_not_raised(
        self, temp_db_path, monkeypatch
    ):
        from apple_mail_mcp.index import manager as manager_mod
        from apple_mail_mcp.index.manager import IndexManager, WriteLock

        manager = IndexManager(db_path=temp_db_path)
        monkeypatch.setattr(manager_mod, "WRITE_LOCK_WAIT", 0.05)

        builder = WriteLock(Path(temp_db_path).with_suffix(".lock"))
        assert builder.acquire(blocking=False)
        try:
            assert manager.delete_email(42) == 0
        finally:
            builder.release()

    def test_it_deletes_when_the_index_is_free(self, temp_db_path):
        from apple_mail_mcp.index.manager import IndexManager

        manager = IndexManager(db_path=temp_db_path)
        conn = manager._get_conn()
        conn.execute(
            "INSERT INTO emails (message_id, account, mailbox, subject) "
            "VALUES (?, ?, ?, ?)",
            (42, "uuid-1", "INBOX", "hello"),
        )
        conn.commit()

        assert manager.delete_email(42, account="uuid-1", mailbox="INBOX") == 1
        assert manager._write_lock.acquire(blocking=False), (
            "the cleanup left the write lock held"
        )
        manager._write_lock.release()


class TestTheLockFileIsItsOwnFile:
    """Documentation and operator both expect `<index>.lock`."""

    def test_the_database_itself_is_not_flocked(self, tmp_path):
        from apple_mail_mcp.index import IndexManager

        db = tmp_path / "index.db"
        mgr = IndexManager(db_path=db)
        assert mgr._write_lock._path != db
        assert mgr._write_lock._path.suffix == ".lock"

    def test_rebuild_takes_the_lock_before_deleting(self, tmp_path):
        """The DELETE ran outside the lock, so a second rebuild could
        wipe rows mid-build — and it surfaced as a raw 'database is
        locked' instead of the IndexBusyError this class exists to
        give."""
        from apple_mail_mcp.index import IndexManager
        from apple_mail_mcp.index.manager import IndexBusyError

        mgr = IndexManager(db_path=tmp_path / "index.db")
        conn = mgr._get_conn()
        conn.execute(
            "INSERT INTO emails (message_id, account, mailbox, subject) "
            "VALUES (1, 'a', 'INBOX', 'keep me')"
        )
        conn.commit()

        # Somebody else is already building.
        assert mgr._write_lock.acquire(blocking=False)
        try:
            with pytest.raises(IndexBusyError):
                mgr.rebuild()
        finally:
            mgr._write_lock.release()

        rows = conn.execute("SELECT COUNT(*) FROM emails").fetchone()[0]
        assert rows == 1, "the refused rebuild deleted rows anyway"


class TestTheLockIsNeverLeaked:
    """The lock is taken before the first thing that can fail.

    Any escape between the acquire and the release — an unreadable mail
    directory, a JXA failure while resolving exclusions, a database that
    will not open — left both the flock and the thread lock held for the
    rest of the process's life. Every later build and sync was then
    refused with "already running" while nothing was running.
    """

    def test_an_unreadable_mail_directory_releases_it(self, temp_db_path):
        from unittest.mock import patch

        from apple_mail_mcp.index.manager import IndexManager

        mgr = IndexManager(db_path=temp_db_path)
        with patch(
            "apple_mail_mcp.index.disk.find_mail_directory",
            side_effect=PermissionError("no Full Disk Access"),
        ):
            assert mgr.sync_updates() == 0

        assert not mgr._write_lock.locked()

    def test_a_failure_resolving_exclusions_releases_it(self, temp_db_path):
        from unittest.mock import patch

        from apple_mail_mcp.index.manager import IndexManager

        mgr = IndexManager(db_path=temp_db_path)
        with (
            patch(
                "apple_mail_mcp.index.disk.find_mail_directory",
                return_value=temp_db_path.parent,
            ),
            patch.object(
                mgr,
                "_resolve_exclusions",
                side_effect=RuntimeError("JXA refused"),
            ),
            pytest.raises(RuntimeError),
        ):
            mgr.sync_updates()

        assert not mgr._write_lock.locked(), (
            "the lock stayed held, so every later sync is refused"
        )


class TestTriggersSurviveAFailureBeforeTheLoop:
    """A dropped trigger outlives the failure that followed it.

    Not because DDL autocommits — sqlite3 holds `DROP TRIGGER` in the
    same implicit transaction as any DML — but because the batch commits
    during the build make it permanent along the way.

    With the drop outside the `try`, a failure in any statement between
    it and the loop — a corrupt FTS table makes `'delete-all'` raise —
    left the database FILE without its triggers. That survives restarts,
    and from then on every insert lands in `emails` but never in
    `emails_fts`: body search silently stops seeing new mail.
    """

    def _trigger_names(self, conn) -> set[str]:
        return {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            )
        }

    def test_a_failure_while_clearing_leaves_the_triggers_in_place(
        self, tmp_path
    ):
        from unittest.mock import patch

        from apple_mail_mcp.index import IndexManager

        mgr = IndexManager(db_path=tmp_path / "idx.db")
        conn = mgr._get_conn()
        assert self._trigger_names(conn) >= {
            "emails_ai",
            "emails_ad",
            "emails_au",
        }

        # Fail after the triggers are dropped, exactly where a corrupt
        # FTS table would. sqlite3.Connection.execute is read-only, so
        # the connection is wrapped rather than patched.
        class Failing:
            def __init__(self, inner):
                self._inner = inner

            def execute(self, sql, *a, **kw):
                if "delete-all" in sql:
                    raise sqlite3.OperationalError("vtable constructor failed")
                return self._inner.execute(sql, *a, **kw)

            def __getattr__(self, name):
                return getattr(self._inner, name)

        with (
            patch.object(mgr, "_get_conn", return_value=Failing(conn)),
            patch(
                "apple_mail_mcp.index.disk.find_mail_directory",
                return_value=tmp_path,
            ),
            patch.object(mgr, "_resolve_exclusions", return_value=set()),
            pytest.raises(sqlite3.OperationalError),
        ):
            mgr.build_from_disk()

        assert self._trigger_names(mgr._get_conn()) >= {
            "emails_ai",
            "emails_ad",
            "emails_au",
        }, "the database file lost its FTS triggers permanently"
