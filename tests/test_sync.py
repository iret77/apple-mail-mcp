"""Tests for disk-based sync functionality (v0.4.0)."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from apple_mail_mcp.index.disk import get_disk_inventory
from apple_mail_mcp.index.schema import SCHEMA_VERSION, get_schema_sql
from apple_mail_mcp.index.sync import (
    SyncResult,
    get_db_inventory,
    sync_from_disk,
)
from apple_mail_mcp.index.watcher import PATH_PATTERN


class TestWatcherPathPattern:
    """Tests for watcher PATH_PATTERN regex (#39)."""

    def test_matches_regular_emlx(self):
        path = "/Users/x/Library/Mail/V10/acc/INBOX.mbox/Data/1/Messages/12345.emlx"
        m = PATH_PATTERN.search(path)
        assert m is not None
        assert m.group(1) == "acc"
        assert m.group(2) == "INBOX"
        assert m.group(3) == "12345"

    def test_matches_partial_emlx(self):
        path = "/Users/x/Library/Mail/V10/acc/INBOX.mbox/Data/1/Messages/67301.partial.emlx"
        m = PATH_PATTERN.search(path)
        assert m is not None
        assert m.group(1) == "acc"
        assert m.group(2) == "INBOX"
        assert m.group(3) == "67301"

    def test_rejects_non_emlx(self):
        path = (
            "/Users/x/Library/Mail/V10/acc/INBOX.mbox/Data/1/Messages/12345.txt"
        )
        assert PATH_PATTERN.search(path) is None


class TestSyncResult:
    """Tests for SyncResult dataclass."""

    def test_total_changes(self):
        result = SyncResult(added=5, deleted=3, moved=2, errors=1)
        assert result.total_changes == 10  # 5 + 3 + 2

    def test_zero_changes(self):
        result = SyncResult(added=0, deleted=0, moved=0, errors=0)
        assert result.total_changes == 0


class TestGetDbInventory:
    """Tests for database inventory function."""

    def test_empty_database(self, temp_db: sqlite3.Connection):
        inventory = get_db_inventory(temp_db)
        assert inventory == {}

    def test_returns_email_paths(self, temp_db: sqlite3.Connection):
        # Insert emails with paths
        temp_db.execute(
            """INSERT INTO emails
               (message_id, account, mailbox, subject, emlx_path)
               VALUES (1, 'acc1', 'INBOX', 'Test', '/path/to/1.emlx')"""
        )
        temp_db.execute(
            """INSERT INTO emails
               (message_id, account, mailbox, subject, emlx_path)
               VALUES (2, 'acc1', 'INBOX', 'Test2', '/path/to/2.emlx')"""
        )
        temp_db.commit()

        inventory = get_db_inventory(temp_db)
        assert len(inventory) == 2
        assert inventory[("acc1", "INBOX", 1)] == "/path/to/1.emlx"
        assert inventory[("acc1", "INBOX", 2)] == "/path/to/2.emlx"

    def test_handles_null_paths(self, temp_db: sqlite3.Connection):
        # Insert email without path (legacy data)
        temp_db.execute(
            """INSERT INTO emails
               (message_id, account, mailbox, subject)
               VALUES (1, 'acc1', 'INBOX', 'Test')"""
        )
        temp_db.commit()

        inventory = get_db_inventory(temp_db)
        assert inventory[("acc1", "INBOX", 1)] == ""


class TestGetDiskInventory:
    """Tests for disk inventory scanning."""

    def test_empty_directory(self, tmp_path: Path):
        mail_dir = tmp_path / "V10"
        mail_dir.mkdir()
        inventory = get_disk_inventory(mail_dir)
        assert inventory == {}

    def test_finds_emlx_files(self, tmp_path: Path):
        # Create mail directory structure
        mail_dir = tmp_path / "V10"
        mbox = mail_dir / "account-uuid" / "INBOX.mbox" / "Data" / "Messages"
        mbox.mkdir(parents=True)

        # Create emlx files
        (mbox / "12345.emlx").write_bytes(b"test")
        (mbox / "67890.emlx").write_bytes(b"test")

        inventory = get_disk_inventory(mail_dir)
        assert len(inventory) == 2
        assert ("account-uuid", "INBOX", 12345) in inventory
        assert ("account-uuid", "INBOX", 67890) in inventory

    def test_includes_partial_files(self, tmp_path: Path):
        """Partial .emlx files are now indexed (#39)."""
        mail_dir = tmp_path / "V10"
        mbox = mail_dir / "acc" / "INBOX.mbox" / "Data" / "Messages"
        mbox.mkdir(parents=True)

        # Create normal and partial files (same message ID)
        (mbox / "12345.emlx").write_bytes(b"test")
        (mbox / "12345.partial.emlx").write_bytes(b"partial")

        inventory = get_disk_inventory(mail_dir)
        # Both map to the same (acc, INBOX, 12345) key — last one wins
        assert ("acc", "INBOX", 12345) in inventory

    def test_handles_nested_mbox_structure(self, tmp_path: Path):
        mail_dir = tmp_path / "V10"
        # Deep nesting: acc/Folder.mbox/Subfolder.mbox/Data/Messages/
        mbox = (
            mail_dir
            / "acc"
            / "Folder.mbox"
            / "Subfolder.mbox"
            / "Data"
            / "Messages"
        )
        mbox.mkdir(parents=True)
        (mbox / "1.emlx").write_bytes(b"test")

        inventory = get_disk_inventory(mail_dir)
        assert len(inventory) == 1


class TestSyncFromDisk:
    """Tests for disk-based state reconciliation."""

    @pytest.fixture
    def sync_db(self, tmp_path: Path):
        """Create a database for sync testing."""
        db_path = tmp_path / "sync_test.db"
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.executescript(get_schema_sql())
        conn.execute(
            "INSERT INTO schema_version (version) VALUES (?)",
            (SCHEMA_VERSION,),
        )
        conn.commit()
        yield conn
        conn.close()

    @pytest.fixture
    def mail_dir(self, tmp_path: Path) -> Path:
        """Create a mock mail directory."""
        mail_dir = tmp_path / "Mail" / "V10"
        mail_dir.mkdir(parents=True)
        return mail_dir

    def _create_emlx(
        self, mail_dir: Path, account: str, mailbox: str, msg_id: int
    ) -> Path:
        """Helper to create a valid emlx file."""
        mbox = mail_dir / account / f"{mailbox}.mbox" / "Data" / "Messages"
        mbox.mkdir(parents=True, exist_ok=True)

        # Create minimal valid emlx
        emlx_path = mbox / f"{msg_id}.emlx"
        content = b"100\nFrom: test@test.com\nSubject: Test\n\nBody text"
        emlx_path.write_bytes(content)
        return emlx_path

    def test_sync_empty_both(self, sync_db: sqlite3.Connection, mail_dir: Path):
        """Sync with empty DB and empty disk should be no-op."""
        result = sync_from_disk(sync_db, mail_dir)
        assert result.added == 0
        assert result.deleted == 0
        assert result.moved == 0

    def test_sync_detects_new_emails(
        self, sync_db: sqlite3.Connection, mail_dir: Path
    ):
        """New files on disk should be added to DB."""
        self._create_emlx(mail_dir, "acc1", "INBOX", 1001)
        self._create_emlx(mail_dir, "acc1", "INBOX", 1002)

        result = sync_from_disk(sync_db, mail_dir)
        assert result.added == 2
        assert result.deleted == 0

        # Verify in DB
        cursor = sync_db.execute("SELECT COUNT(*) FROM emails")
        assert cursor.fetchone()[0] == 2

    def test_sync_detects_deleted_emails(
        self, sync_db: sqlite3.Connection, mail_dir: Path
    ):
        """Emails in DB but not on disk should be deleted."""
        # Pre-populate DB with an email that doesn't exist on disk
        sync_db.execute(
            """INSERT INTO emails
               (message_id, account, mailbox, subject, emlx_path)
               VALUES (999, 'ghost', 'INBOX', 'Deleted', '/gone.emlx')"""
        )
        sync_db.commit()

        result = sync_from_disk(sync_db, mail_dir)
        assert result.deleted == 1

        # Verify removed from DB
        cursor = sync_db.execute("SELECT COUNT(*) FROM emails")
        assert cursor.fetchone()[0] == 0

    def test_sync_detects_moved_emails(
        self, sync_db: sqlite3.Connection, mail_dir: Path
    ):
        """Emails with changed paths should be updated."""
        # Create file at new location
        new_path = self._create_emlx(mail_dir, "acc1", "Archive", 1001)

        # Pre-populate DB with old path
        sync_db.execute(
            """INSERT INTO emails
               (message_id, account, mailbox, subject, emlx_path)
               VALUES (1001, 'acc1', 'Archive', 'Moved', '/old/path.emlx')"""
        )
        sync_db.commit()

        result = sync_from_disk(sync_db, mail_dir)
        assert result.moved == 1

        # Verify path updated
        cursor = sync_db.execute(
            "SELECT emlx_path FROM emails WHERE message_id = 1001"
        )
        assert str(new_path) in cursor.fetchone()[0]

    def test_sync_sorts_new_by_mtime(
        self, sync_db: sqlite3.Connection, mail_dir: Path
    ):
        """With cap=1, the newer file should be indexed."""
        import os
        import time

        # Create older file first
        older = self._create_emlx(mail_dir, "acc1", "INBOX", 1001)
        time.sleep(0.05)
        # Create newer file
        self._create_emlx(mail_dir, "acc1", "INBOX", 1002)

        # Make sure mtime differs
        os.utime(older, (time.time() - 100, time.time() - 100))

        with patch(
            "apple_mail_mcp.index.sync.get_index_max_emails",
            return_value=1,
        ):
            result = sync_from_disk(sync_db, mail_dir)

        # Only 1 should be indexed (the newer one, msg 1002)
        assert result.added == 1
        cursor = sync_db.execute("SELECT message_id FROM emails")
        msg_id = cursor.fetchone()["message_id"]
        assert msg_id == 1002

    def test_sync_logs_cap_warning(
        self, sync_db: sqlite3.Connection, mail_dir: Path, caplog
    ):
        """Sync logs an aggregate cap warning when mailboxes hit limit."""
        import logging

        # Create more files than the cap
        for i in range(3):
            self._create_emlx(mail_dir, "acc1", "INBOX", 2000 + i)

        with (
            patch(
                "apple_mail_mcp.index.sync.get_index_max_emails",
                return_value=1,
            ),
            caplog.at_level(logging.WARNING),
        ):
            sync_from_disk(sync_db, mail_dir)

        assert "hit cap" in caplog.text


class TestOneBadMessageCannotAbortTheSync:
    """A single unusable message must land in the DLQ, not abort the
    run: an undecodable header stopped every sync for a full day."""

    def test_attribute_error_from_one_file_is_isolated(
        self, temp_db, tmp_path, monkeypatch
    ):
        from unittest.mock import patch

        from apple_mail_mcp.index import sync as sync_mod

        good = tmp_path / "1.emlx"
        bad = tmp_path / "2.emlx"
        for f in (good, bad):
            f.write_bytes(b"x")

        # iter_disk_inventory yields flat rows for the temp table.
        inventory = [
            ("acct", "INBOX", 1, str(good)),
            ("acct", "INBOX", 2, str(bad)),
        ]

        def fake_parse(path):
            if str(path) == str(bad):
                # Exactly what an undecodable header did.
                raise AttributeError("'Header' object has no attribute 'strip'")
            return SimpleNamespace(
                id=1,
                subject="ok",
                sender="a@b",
                content="c",
                date_received="2026-01-01",
                message_id_header="<1@x>",
                attachments=[],
            )

        with (
            # iter_disk_inventory and parse_emlx are imported inside the
            # function, so they must be patched at their source module.
            patch(
                "apple_mail_mcp.index.disk.iter_disk_inventory",
                return_value=iter(inventory),
            ),
            patch("apple_mail_mcp.index.disk.parse_emlx", fake_parse),
            patch.object(sync_mod, "emlx_too_large", lambda p: False),
        ):
            result = sync_mod.sync_from_disk(temp_db, tmp_path)

        # The good one is indexed, the bad one is recorded, no raise.
        assert result.added == 1
        row = temp_db.execute(
            "SELECT error_type FROM failed_index_jobs WHERE emlx_path = ?",
            (str(bad),),
        ).fetchone()
        assert row is not None and row[0] == "AttributeError"


class TestAnUnparseableFileIsRecordedToo:
    """`parse_emlx()` answers a file it cannot make sense of with None
    rather than an exception, so it never reached the DLQ path — the
    message was absent from the index with nothing to explain the gap,
    one branch away from the silence this unit removes."""

    def test_a_none_result_lands_in_the_dlq(self, temp_db, tmp_path):
        from unittest.mock import patch

        from apple_mail_mcp.index.sync import sync_from_disk

        mail_dir = tmp_path / "V10"
        box = mail_dir / "ACCT" / "INBOX.mbox" / "Data" / "Messages"
        box.mkdir(parents=True)
        (box / "1.emlx").write_bytes(b"12\nnot a message")

        with patch("apple_mail_mcp.index.disk.parse_emlx", return_value=None):
            sync_from_disk(temp_db, mail_dir)

        rows = temp_db.execute(
            "SELECT emlx_path FROM failed_index_jobs"
        ).fetchall()
        assert len(rows) == 1, (
            "the message vanished from the index with no record"
        )


class TestAPartialInsertIsNotLeftBehind:
    """A failure AFTER the email row went in leaves a half-indexed
    message: the row exists, so the next sync sees the id in the DB
    inventory and never revisits it. Its attachments stay missing
    forever, while the DLQ row claims it will be retried."""

    def test_the_row_is_removed_so_the_next_sync_retries(
        self, temp_db, tmp_path
    ):
        from unittest.mock import patch

        from apple_mail_mcp.index.sync import sync_from_disk

        mail_dir = tmp_path / "V10"
        box = mail_dir / "ACCT" / "INBOX.mbox" / "Data" / "Messages"
        box.mkdir(parents=True)
        emlx = box / "1.emlx"
        mime = (
            b"From: a@b.com\r\nSubject: s\r\n"
            b"Date: Mon, 1 Jan 2026 10:00:00 +0100\r\n"
            b'Content-Type: multipart/mixed; boundary="B"\r\n\r\n--B\r\n'
            b"Content-Type: text/plain\r\n\r\nbody\r\n--B\r\n"
            b"Content-Type: application/pdf\r\n"
            b'Content-Disposition: attachment; filename="a.pdf"\r\n\r\n'
            b"XX\r\n--B--\r\n"
        )
        emlx.write_bytes(f"{len(mime)}\n".encode() + mime + b"<plist/>")

        # The email row goes in; the attachments do not.
        with patch(
            "apple_mail_mcp.index.sync.insert_attachments",
            side_effect=sqlite3.ProgrammingError("bad parameter"),
        ):
            sync_from_disk(temp_db, mail_dir)

        rows = temp_db.execute("SELECT COUNT(*) FROM emails").fetchone()[0]
        assert rows == 0, (
            "a half-indexed message stayed in the index, so the next "
            "sync will skip it and its attachments are lost for good"
        )
        dlq = temp_db.execute(
            "SELECT COUNT(*) FROM failed_index_jobs"
        ).fetchone()[0]
        assert dlq == 1


class TestOversizedIsRecordedEvenAtTheCap:
    """The size check has to run before the mailbox cap.

    A mailbox already at MAX_EMAILS swallowed the file with no DLQ row —
    the exact silence this unit removes, restored by ordering. And a
    size skip must not be counted as a cap skip: the remedies differ,
    and "hit cap" advises raising a limit that is not the problem.
    """

    def test_a_capped_mailbox_still_records_the_size_skip(
        self, temp_db, tmp_path, monkeypatch
    ):
        from apple_mail_mcp.index.sync import sync_from_disk

        mail_dir = tmp_path / "V10"
        box = mail_dir / "ACCT" / "INBOX.mbox" / "Data" / "Messages"
        box.mkdir(parents=True)
        payload = b"x" * (2 * 1024 * 1024)
        (box / "1.emlx").write_bytes(f"{len(payload)}\n".encode() + payload)

        monkeypatch.setenv("APPLE_MAIL_INDEX_MAX_EMAIL_MB", "1")
        # Cap of zero: every mailbox counts as already full.
        monkeypatch.setenv("APPLE_MAIL_INDEX_MAX_EMAILS", "0")

        sync_from_disk(temp_db, mail_dir)

        rows = temp_db.execute(
            "SELECT error_message FROM failed_index_jobs"
        ).fetchall()
        assert [r[0] for r in rows] == ["too_large"], (
            "the oversized message vanished because the mailbox was capped"
        )


class TestAnIndexWrittenWithGuidNamesHealsItself:
    """0.20.2/0.20.3 wrote `INBOX/<GUID>` where `INBOX` belongs. The
    release notes promise the next sync repairs that — a claim worth a
    test, since two of the last three defects were unverified claims."""

    def test_the_next_sync_replaces_the_poisoned_row(self, tmp_path):
        from apple_mail_mcp.index.manager import IndexManager
        from apple_mail_mcp.index.sync import sync_from_disk

        guid = "00000000-0000-0000-0000-000000000000"
        mail = tmp_path / "V10"
        msgs = mail / "UUID-A" / "INBOX.mbox" / guid / "Data" / "1" / "Messages"
        msgs.mkdir(parents=True)
        (msgs / "4711.emlx").write_text(
            "120\nFrom: a@b\nSubject: Test\nMessage-ID: <t@x>\n\nBody\n"
        )

        manager = IndexManager(db_path=tmp_path / "i.db")
        conn = manager._get_conn()
        conn.execute(
            "INSERT INTO emails (message_id, account, mailbox, subject,"
            " emlx_path, rfc822_message_id) VALUES (?,?,?,?,?,?)",
            (
                4711,
                "UUID-A",
                f"INBOX/{guid}",  # what 0.20.2/0.20.3 wrote
                "Test",
                str(msgs / "4711.emlx"),
                "<t@x>",
            ),
        )
        conn.commit()

        sync_from_disk(conn, mail)

        names = [r[0] for r in conn.execute("SELECT mailbox FROM emails")]
        assert names == ["INBOX"], (
            f"the poisoned row survived the sync: {names}"
        )
